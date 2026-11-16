"use strict";
import {$, action, api, deleteMedia, formatTime, initSession, notice, petNames} from "/assets/client.js";
const form = $("#settings-form");
const names = {eating: "吃喝", destruction: "拆家", toileting: "乱拉乱尿", on_sofa: "上沙发", on_table: "上桌子", on_chair: "上椅子"};
const pets = {dog: "百万", cat: "英镑", both: "百万和英镑", unknown: "宠物待确认"};
const states = {confirmed: "已确认", rejected: "复核未成立", unreviewed: "未复核 · 间隔限制", reviewing: "复核中", review_error: "复核失败", cancelled: "已中断"};
const notifications = {none: "未通知", record_only: "只记录", deduplicated: "五分钟内已提醒", not_configured: "未配置 Bark", sending: "发送中", sent: "图文通知已发送", sent_text: "文字通知已发送", failed: "通知失败", unknown: "通知结果待确认"};
const phases = {starting: "正在启动", disabled: "守护已暂停", needs_camera: "等待摄像头", outside_schedule: "当前不在守护时段", waiting: "等待下次采样", recording: "正在录制", deciding: "正在初判", reviewing: "正在复核", no_event: "本轮未发现事件", review_cooldown: "候选已记录，等待复核间隔", complete: "本轮处理完成", error: "处理遇到问题"};
let config = null, before = null, browsingOlder = false;
let eventGeneration = 0, deletingEventMedia = false;
const selectedEvents = new Set();

function addWindow(start = "09:00", end = "18:00") {
  if ($("#windows").children.length >= 24) { notice("最多添加 24 个时段", true); return; }
  const node = $("#window-template").content.cloneNode(true);
  node.querySelector(".window-start").value = start;
  node.querySelector(".window-end").value = end;
  node.querySelector("button").addEventListener("click", e => e.target.closest(".window-row").remove());
  $("#windows").append(node);
}

function fillConfig(value) {
  config = value;
  for (const key of ["sample_fps", "clip_duration", "speed", "review_interval", "retention_days", "model_url", "public_url"]) form.elements[key].value = value[key];
  for (const key of Object.keys(names)) form.elements[key].value = value.event_modes[key] || "off";
  for (const key of ["stream_url", "model_key", "bark_url"]) {
    form.elements[key].value = "";
    form.elements[key].placeholder = value[key + "_configured"] ? "已保存 · 留空保留" : "尚未配置";
  }
  $("#windows").replaceChildren();
  value.windows.forEach(w => addWindow(w.start, w.end));
  $("#toggle").textContent = config.enabled ? "暂停守护" : "开启守护";
  $("#toggle").disabled = false;
}

async function status() {
  const result = await api("/api/status");
  $("#phase").textContent = result.recording ? "持续采集中" : phases[result.phase] || result.phase;
  $("#last-error").textContent = result.last_error || (result.recording ? (phases[result.phase] || "正在录制") + " · 待分析 " + result.queued + " 段"
    : result.phase === "needs_camera" ? "请先保存视频源地址并测试连接" : "北京时间 · 连续采集，分段分析");
  $("#last-error").classList.toggle("error-text", Boolean(result.last_error));
  $("#last-started").textContent = formatTime(result.last_started);
  $("#last-review").textContent = formatTime(result.last_review * 1000);
  $("#next-sample").textContent = result.last_capture ? "已采集至 " + formatTime(result.last_capture) : "时间均为北京时间";
  if (config) { config.enabled = result.enabled; $("#toggle").textContent = result.enabled ? "暂停守护" : "开启守护"; }
}

async function loadEvents(older = false) {
  if (deletingEventMedia) return;
  const revision = ++eventGeneration;
  const result = await api("/api/events" + (older && before ? "?before=" + before : ""));
  if (revision !== eventGeneration) return;
  const list = $("#event-list");
  if (!older) { list.replaceChildren(); browsingOlder = false; }
  else browsingOlder = true;
  if (!result.items.length && !older) { const p = document.createElement("p"); p.className = "empty"; p.textContent = "还没有事件。开启守护后，初判候选和复核结果会出现在这里。"; list.append(p); }
  for (const event of result.items) {
    const fragment = $("#event-template").content.cloneNode(true);
    fragment.querySelector("article").dataset.eventId = event.id;
    const select = fragment.querySelector(".event-select"); select.value = event.id;
    select.checked = selectedEvents.has(event.id); select.disabled = !event.images.length;
    fragment.querySelector("h3").textContent = (names[event.type] || event.type) + " · " + pets[event.pet];
    const badge = fragment.querySelector(".badge");
    badge.textContent = states[event.status] || event.status;
    badge.classList.toggle("confirmed", event.status === "confirmed");
    const time = fragment.querySelector("time");
    time.dateTime = event.occurred_at;
    time.textContent = formatTime(event.occurred_at) + " · 北京时间采样开始";
    fragment.querySelector(".description").textContent = petNames(event.description);
    fragment.querySelector(".event-meta").textContent = "初判 " + Math.round(event.score * 100) + "% · " + (notifications[event.notification] || event.notification) + (event.images.length ? "" : " · 媒体已删除或过期");
    event.images.forEach((_, i) => {
      const link = document.createElement("a"), image = document.createElement("img");
      link.href = "/api/events/" + event.id + "/images/" + i;
      link.target = "_blank"; link.rel = "noopener";
      image.src = link.href; image.loading = "lazy";
      image.alt = "原视频第 " + event.frame_offsets[i] + " 秒截图";
      link.append(image); fragment.querySelector(".event-images").append(link);
    });
    list.append(fragment);
  }
  before = result.next_before;
  $("#older").hidden = !before;
  updateEventSelection();
}

function updateEventSelection() {
  const boxes = [...document.querySelectorAll(".event-select")];
  const available = boxes.filter(box => !box.disabled);
  for (const id of selectedEvents) if (!available.some(box => Number(box.value) === id)) selectedEvents.delete(id);
  const all = $("#select-all-events");
  all.checked = available.length > 0 && selectedEvents.size === available.length;
  all.indeterminate = selectedEvents.size > 0 && selectedEvents.size < available.length;
  all.disabled = deletingEventMedia || !available.length;
  $("#delete-selected-events").textContent = "删除选中 (" + selectedEvents.size + ")";
  $("#delete-selected-events").disabled = deletingEventMedia || !selectedEvents.size;
  for (const box of boxes) box.closest("article").querySelector(".delete-event-media").disabled = deletingEventMedia || box.disabled;
}

async function removeEventMedia(ids) {
  deletingEventMedia = true; eventGeneration++;
  updateEventSelection();
  try {
    const result = await deleteMedia("events", ids);
    if (!result) return;
    for (const id of result.events) {
      selectedEvents.delete(id);
      const card = document.querySelector('[data-event-id="' + id + '"]');
      if (!card) continue;
      card.querySelector(".event-images").replaceChildren();
      const box = card.querySelector(".event-select"); box.checked = false; box.disabled = true;
      card.querySelector(".event-meta").textContent += " · 媒体已删除";
    }
  } finally { deletingEventMedia = false; updateEventSelection(); }
}

$("#event-list").addEventListener("change", event => {
  if (!event.target.matches(".event-select")) return;
  const id = Number(event.target.value);
  if (event.target.checked) selectedEvents.add(id); else selectedEvents.delete(id);
  updateEventSelection();
});
$("#select-all-events").addEventListener("change", event => {
  for (const box of document.querySelectorAll(".event-select:not(:disabled)")) {
    box.checked = event.target.checked;
    if (box.checked) selectedEvents.add(Number(box.value)); else selectedEvents.delete(Number(box.value));
  }
  updateEventSelection();
});
$("#event-list").addEventListener("click", event => {
  const button = event.target.closest(".delete-event-media");
  if (button) action(button, () => removeEventMedia([Number(button.closest("article").dataset.eventId)])).then(updateEventSelection);
});
$("#delete-selected-events").addEventListener("click", event => {
  action(event.currentTarget, () => removeEventMedia([...selectedEvents])).then(updateEventSelection);
});

form.addEventListener("submit", e => {
  e.preventDefault();
  action($("#save"), async () => {
    const payload = {};
    for (const key of ["sample_fps", "clip_duration", "speed", "review_interval", "retention_days"]) payload[key] = Number(form.elements[key].value);
    for (const key of ["model_url", "public_url"]) payload[key] = form.elements[key].value.trim();
    for (const key of ["stream_url", "model_key", "bark_url"]) if (form.elements[key].value.trim()) payload[key] = form.elements[key].value.trim();
    payload.windows = [...document.querySelectorAll(".window-row")].map(row => ({start: row.querySelector(".window-start").value, end: row.querySelector(".window-end").value}));
    payload.event_modes = Object.fromEntries(Object.keys(names).map(key => [key, form.elements[key].value]));
    fillConfig(await api("/api/config", "PUT", payload));
    notice("设置已保存并生效。"); await status();
  });
});
$("#password-form").addEventListener("submit", event => {
  event.preventDefault();
  const passwordForm = event.currentTarget;
  action($("#change-password"), async () => {
    const fields = passwordForm.elements;
    $("#password-message").textContent = "";
    try {
      if (fields.new_password.value !== fields.confirm_password.value) throw new Error("两次新密码不一致");
      await api("/api/password", "PUT", {current_password: fields.current_password.value, new_password: fields.new_password.value});
      passwordForm.reset();
      window.location.assign("/login");
    } catch (error) { $("#password-message").textContent = error.message; }
  });
});
$("#toggle").addEventListener("click", e => action(e.target, async () => {
  const result = await api("/api/config", "PUT", {enabled: !config.enabled});
  config.enabled = result.enabled;
  notice(config.enabled ? "已开启守护。" : "守护已暂停。"); await status();
}));
$("#add-window").addEventListener("click", () => addWindow());
$("#test-camera").addEventListener("click", e => action(e.target, async () => {
  notice("正在取流，请稍候。测试使用已保存的视频源。");
  const result = await api("/api/test/camera", "POST");
  $("#preview").src = result.image + "?t=" + Date.now(); $("#preview").hidden = false;
  notice("摄像头连接成功，已取得原视频截图。");
}));
$("#test-bark").addEventListener("click", e => action(e.target, async () => {
  await api("/api/test/bark", "POST"); notice("测试消息已发送，请检查手机。使用的是已保存的 Bark 地址。");
}));
$("#logout").addEventListener("click", e => action(e.target, async () => { await api("/api/logout", "POST"); window.location.assign("/login"); }));
$("#refresh").addEventListener("click", e => action(e.target, () => loadEvents()));
$("#older").addEventListener("click", e => action(e.target, () => loadEvents(true)));
(async () => {
  try {
    await initSession();
    fillConfig(await api("/api/config"));
    await Promise.all([status(), loadEvents()]);
    setInterval(() => { if (!document.hidden) status().catch(e => notice(e.message, true)); }, 5000);
    setInterval(() => { if (!document.hidden && !browsingOlder && !selectedEvents.size) loadEvents().catch(e => notice(e.message, true)); }, 15000);
  } catch (error) { notice(error.message, true); }
})();
