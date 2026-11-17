"use strict";
import {$, formatTime} from "/assets/client.js";

async function loadShared() {
  const match = window.location.pathname.match(/^\/share\/(\d+)$/);
  if (!match) {
    $("#share-title").textContent = "分享链接无效";
    $("#share-missing").hidden = false;
    return;
  }
  const metaUrl = "/share/" + match[1] + "/meta" + window.location.search;
  try {
    const response = await fetch(metaUrl);
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "分享视频不可用");
    const title = result.pet_name + " · " + result.activity_label;
    document.title = title + " · Million Pound";
    $("#share-title").textContent = title;
    $("#share-period").textContent = formatTime(result.started_at) + " — " + formatTime(result.ended_at) + "（北京时间）";
    $("#share-footnote").textContent = "链接有效至 " + formatTime(result.expires_at) + "（北京时间）· 加速录像仅供参考";
    const video = $("#share-video");
    const image = $("#share-image");
    if (result.poster) image.src = result.poster;
    video.addEventListener("error", () => {
      if (!video.error) return;
      video.hidden = true;
      if (result.poster) {
        image.hidden = false;
        $("#share-status").textContent = "视频暂时无法播放，显示静态截图。";
        $("#share-status").hidden = false;
      } else {
        $("#share-missing").hidden = false;
      }
    });
    image.addEventListener("error", () => {
      image.hidden = true;
      if (video.hidden) $("#share-missing").hidden = false;
    });
    if (result.poster) video.poster = result.poster;
    video.src = result.video;
    video.hidden = false;
    video.play().catch(() => {});
  } catch (error) {
    $("#share-title").textContent = "无法打开分享视频";
    $("#share-missing").textContent = error.message;
    $("#share-missing").hidden = false;
  }
}

loadShared();
