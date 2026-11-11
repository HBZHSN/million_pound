export const PETS = {dog: "百万", cat: "英镑"};

export function activityKey(item, pet) {
  if (item.status === "pending") return "pending";
  if (item.status !== "done") return "unknown";
  const decision = item.pets[pet];
  const activity = decision?.activity || "uncertain";
  if (activity === "sleeping" && (!decision.visibility || !decision.identity_verified)) return "unknown";
  if (activity === "uncertain" || activity === "not_visible") return "unknown";
  return activity === "drinking" ? "eating" : activity;
}

export function groupActivities(items) {
  const sorted = [...items].sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at) || a.id - b.id);
  const episodes = [];
  for (const pet of Object.keys(PETS)) {
    let previous = null;
    for (const item of sorted) {
      const key = activityKey(item, pet);
      if (key === "pending") { previous = null; continue; }
      const start = Date.parse(item.started_at), end = Date.parse(item.ended_at);
      // shortcut: 一秒内的帧间隔视为相邻，需精确空档时改用采集会话边界。
      if (previous && previous.key === key && start - previous.end <= 1000) {
        previous.end = Math.max(previous.end, end);
        previous.records.push(item);
      } else {
        previous = {id: pet + "-" + item.id, pet, key, start, end, records: [item]};
        episodes.push(previous);
      }
    }
  }
  return episodes.sort((a, b) => b.end - a.end || a.pet.localeCompare(b.pet));
}

export function timelineMoments(episodes) {
  const moments = new Map();
  for (const episode of episodes) {
    if (!moments.has(episode.end)) moments.set(episode.end, {time: episode.end});
    moments.get(episode.end)[episode.pet] = episode;
  }
  return [...moments.values()].sort((a, b) => b.time - a.time);
}

export function durationLabel(milliseconds) {
  if (milliseconds < 60000) return "片刻";
  const minutes = Math.round(milliseconds / 60000);
  if (minutes < 60) return "约 " + minutes + " 分钟";
  const hours = Math.floor(minutes / 60), remainder = minutes % 60;
  return "约 " + hours + " 小时" + (remainder ? " " + remainder + " 分钟" : "");
}
