"use strict";
export const $ = (selector) => document.querySelector(selector);
let csrf = "";

export function petNames(text) {
  return text.replace(/拉布拉多(?:犬)?/g, "百万").replace(/缅因猫?/g, "英镑");
}

export const formatTime = (value) => value ? new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
}).format(new Date(value)) : "—";

export function notice(text, error = false) {
  const element = $("dialog[open] .media-message") || $("#message");
  element.textContent = text;
  element.classList.toggle("error", error);
  element.hidden = !text;
}

export async function deleteMedia(collection, ids) {
  if (!ids.length) return null;
  if (ids.length > 5000) throw new Error("每次最多删除 5000 条，请分批选择");
  if (!window.confirm("永久删除这 " + ids.length + " 条记录的图片和视频？\n同段录像在时间线、关联事件和分享链接中的媒体会一并删除，文字记录保留。此操作无法恢复。")) return null;
  const result = await api("/api/" + collection + "/media", "DELETE", {ids});
  notice("图片和视频已删除，文字记录保留。");
  return result;
}

export async function api(path, method = "GET", body) {
  const response = await fetch(path, {method, headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: body === undefined ? undefined : JSON.stringify(body)});
  if (response.status === 401) { window.location.assign("/login"); throw new Error("登录已过期"); }
  const result = await response.json();
  if (!response.ok) {
    const detail = result.detail;
    throw new Error(Array.isArray(detail) ? detail.map(e => e.loc.join(".") + "：" + e.msg).join("；") : detail || "请求失败");
  }
  return result;
}

export async function initSession() {
  csrf = (await api("/api/session")).csrf;
}

export async function action(button, work) {
  button.disabled = true;
  try { await work(); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
}
