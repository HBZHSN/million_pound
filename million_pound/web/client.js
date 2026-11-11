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
  const element = $("#message");
  element.textContent = text;
  element.classList.toggle("error", error);
  element.hidden = !text;
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
