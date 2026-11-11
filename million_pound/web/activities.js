"use strict";
import {$, action, api, formatTime, initSession, notice} from "/assets/client.js";

const statuses = {pending: "正在判断", done: "已判断", error: "判断失败", skipped: "积压未判断", cancelled: "已中断"};
let before = null, browsingOlder = false, generation = 0;
$("#day").value = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
for (let hour = 0; hour < 24; hour++) {
  const option = document.createElement("option");
  option.value = hour;
  option.textContent = String(hour).padStart(2, "0") + ":00 — " + String(hour + 1).padStart(2, "0") + ":00";
  $("#hour").append(option);
}

function petState(item, pet, labels) {
  const decision = item.pets[pet];
  if (item.status !== "done" || !decision) return [statuses[item.status] || "判断不清", ""];
  return [labels[decision.activity] || "判断不清", "模型置信度 " + Math.round(decision.confidence * 100) + "%"];
}

function showSummary(item, labels) {
  for (const pet of ["dog", "cat"]) {
    const card = document.querySelector('.pet-summary[data-pet="' + pet + '"]');
    card.hidden = Boolean($("#pet").value && $("#pet").value !== pet);
    const [label, confidence] = item ? petState(item, pet, labels) : ["暂无记录", "所选时段没有记录"];
    card.querySelector(".pet-state").textContent = label;
    card.querySelector(".pet-confidence").textContent = confidence;
  }
  $("#summary-time").textContent = item ? "所选时段最近判断：" + formatTime(item.started_at) + " — " + formatTime(item.ended_at) + "（北京时间）" : "所选时段内没有活动记录。";
}

async function loadActivities(older = false) {
  if (!$("#day").value) { notice("请选择日期", true); return; }
  const revision = ++generation;
  const params = new URLSearchParams({day: $("#day").value});
  if ($("#hour").value !== "") params.set("hour", $("#hour").value);
  if (older && before) params.set("before", before);
  const result = await api("/api/activities?" + params);
  if (revision !== generation) return;
  const list = $("#activity-list");
  const existing = new Map([...list.querySelectorAll(".activity")].map(node => [Number(node.dataset.id), node]));
  const cards = document.createDocumentFragment();
  if (!older) {
    browsingOlder = false;
    showSummary(result.items.find(item => item.status === "done") || result.items[0], result.labels);
  } else browsingOlder = true;
  if (!result.items.length && !older) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这个时段还没有记录。开启守护后，每段录像的活动判断与截图会出现在这里。";
    cards.append(empty);
  }
  for (const item of result.items) {
    const signature = JSON.stringify([item, $("#pet").value]);
    const previous = existing.get(item.id);
    if (previous && previous.dataset.signature === signature) { cards.append(previous); continue; }
    const fragment = $("#activity-template").content.cloneNode(true);
    fragment.querySelector("article").dataset.id = item.id;
    fragment.querySelector("article").dataset.signature = signature;
    for (const [selector, timestamp] of [[".range-start", item.started_at], [".range-end", item.ended_at]]) {
      const time = fragment.querySelector(selector);
      time.dateTime = timestamp; time.textContent = formatTime(timestamp);
    }
    const badge = fragment.querySelector(".badge");
    badge.textContent = statuses[item.status] || item.status;
    badge.classList.toggle("confirmed", item.status === "done");
    for (const pet of ["dog", "cat"]) {
      const row = fragment.querySelector('.activity-pet[data-pet="' + pet + '"]');
      row.hidden = Boolean($("#pet").value && $("#pet").value !== pet);
      const [label, confidence] = petState(item, pet, result.labels);
      row.querySelector(".activity-label").textContent = label;
      row.querySelector(".activity-confidence").textContent = confidence;
    }
    const message = fragment.querySelector(".activity-message");
    message.textContent = item.message; message.hidden = !item.message;
    if (item.images.length) {
      const photo = fragment.querySelector(".activity-photo");
      photo.hidden = false; photo.href = "/api/activities/" + item.id + "/images/0";
      const image = photo.querySelector("img");
      image.src = photo.href;
      const when = new Date(new Date(item.started_at).getTime() + item.frame_offsets[0] * 1000).toISOString();
      image.alt = "北京时间 " + formatTime(when) + " 的原视频截图";
      photo.querySelector(".photo-caption").textContent = "采集约 " + formatTime(when) + " · 点击查看原图";
    } else {
      const missing = fragment.querySelector(".missing-evidence");
      missing.hidden = false;
      missing.textContent = item.status === "pending" ? "正在生成截图…" : "该时段截图已过期或未能生成";
    }
    cards.append(fragment);
  }
  if (older) list.append(cards);
  else list.replaceChildren(cards);
  before = result.next_before;
  $("#older-activities").hidden = !before;
}

async function loadStatus() {
  const status = await api("/api/status");
  $("#capture-status").textContent = status.recording ? "持续采集 · 待分析 " + status.queued + " 段" : status.enabled ? "等待采集" : "守护已暂停";
  if (status.last_error) $("#capture-status").textContent += " · " + status.last_error;
}

$("#timeline-filters").addEventListener("submit", event => {
  event.preventDefault(); action($("#refresh-activities"), () => loadActivities());
});
for (const selector of ["#day", "#hour", "#pet"]) $(selector).addEventListener("change", () => action($("#refresh-activities"), () => loadActivities()));
$("#older-activities").addEventListener("click", event => action(event.target, () => loadActivities(true)));
$("#logout").addEventListener("click", event => action(event.target, async () => { await api("/api/logout", "POST"); window.location.assign("/login"); }));
(async () => {
  try {
    await initSession();
    await Promise.all([loadActivities(), loadStatus()]);
    setInterval(() => {
      if (!document.hidden) Promise.all([loadStatus(), browsingOlder ? Promise.resolve() : loadActivities()]).catch(error => notice(error.message, true));
    }, 5000);
  } catch (error) { notice(error.message, true); }
})();
