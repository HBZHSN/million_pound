export const PETS = {dog: "百万", cat: "英镑"};
const SUSPECT_PREFIX = "suspect:";
const ACTIVITY_PRIORITY = {
  toileting: 0,
  destruction: 1,
  eating: 2,
  on_table: 3,
  on_sofa: 4,
  on_chair: 5,
  playing: 6,
  walking: 7,
  grooming: 8,
  sleeping: 9,
};
const DEFAULT_PRIORITY = 10;
const CANDIDATES = new Set([
  "sleeping", "resting", "eating", "drinking", "walking", "playing",
  "grooming", "on_sofa", "on_table", "on_chair", "other",
]);
const normalizeActivity = activity => activity === "resting" ? "sleeping" : activity === "drinking" ? "eating" : activity;

export function activityRank(key) {
  if (typeof key !== "string") return (DEFAULT_PRIORITY + 1) * 2;
  const suspected = key.startsWith(SUSPECT_PREFIX);
  const base = suspected ? key.slice(SUSPECT_PREFIX.length) : key;
  return (ACTIVITY_PRIORITY[base] ?? DEFAULT_PRIORITY) * 2 + Number(suspected);
}

export function activityCandidate(decision) {
  const candidates = Object.entries(decision?.probabilities || {}).filter(([activity, score]) =>
    CANDIDATES.has(activity) && Number.isFinite(score) && score >= 0 && score <= 1);
  return candidates.sort((a, b) => b[1] - a[1] || activityRank(normalizeActivity(a[0])) - activityRank(normalizeActivity(b[0])))[0]?.[0] ||
    (CANDIDATES.has(decision?.proposed_activity) ? decision.proposed_activity : null);
}

export function activityKey(item, pet) {
  if (item.status === "pending") return "pending";
  if (item.status !== "done") return null;
  const decision = item.pets[pet];
  if (decision?.visibility !== "present" || decision.activity === "not_visible" || decision.proposed_activity === "not_visible") return null;
  let key = null;
  const activity = decision.activity;
  if (CANDIDATES.has(activity)) key = normalizeActivity(activity);
  else if (activity === "uncertain") {
    const candidate = activityCandidate(decision);
    if (candidate) key = SUSPECT_PREFIX + normalizeActivity(candidate);
  }
  for (const event of item.events || []) {
    if ((event.pet !== pet && event.pet !== "both") || !CANDIDATES.has(event.type)) continue;
    const normalized = normalizeActivity(event.type);
    if (key === null || activityRank(normalized) < activityRank(key)) key = normalized;
  }
  return key;
}

export function groupActivities(items) {
  const sorted = [...items].sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at) || a.id - b.id);
  const episodes = [];
  for (const pet of Object.keys(PETS)) {
    let previous = null, gaps = [];
    for (const item of sorted) {
      const key = activityKey(item, pet);
      const start = Date.parse(item.started_at), end = Date.parse(item.ended_at);
      const continuityEnd = gaps.length ? Date.parse(gaps.at(-1).ended_at) : previous?.end;
      if (key === "pending" || key === null) {
        const decision = item.pets[pet];
        // 旧版入镜门槛可能丢掉活动答案，只连接两侧仍睡觉的一分钟内短缺口。
        if (previous?.key.replace(SUSPECT_PREFIX, "") === "sleeping" && item.status === "done" &&
            decision?.visibility === "present" && decision.activity === "uncertain" &&
            !decision.proposed_activity && !decision.probabilities &&
            start - continuityEnd <= 1000 && end - previous.end <= 60000) gaps.push(item);
        else { previous = null; gaps = []; }
        continue;
      }
      // 确认程度变化不切断同一状态，原始判断仍保留在 records 中。
      if (previous && previous.key.replace(SUSPECT_PREFIX, "") === key.replace(SUSPECT_PREFIX, "") &&
          start - continuityEnd <= 1000 && (!gaps.length || start - previous.end <= 60000)) {
        if (!key.startsWith(SUSPECT_PREFIX)) previous.key = key;
        previous.end = Math.max(previous.end, end);
        previous.records.push(...gaps, item);
      } else {
        previous = {id: pet + "-" + item.id, pet, key, start, end, records: [item]};
        episodes.push(previous);
      }
      gaps = [];
    }
  }
  return episodes.sort((a, b) => b.end - a.end || a.pet.localeCompare(b.pet));
}

export function timelinePhotos(items, ticks) {
  if (ticks.length < 2) return [];
  const step = ticks[0].time - ticks[1].time;
  const cells = new Map(ticks.slice(1).map((tick, index) => [tick.time, (tick.top + ticks[index].top) / 2]));
  const photos = new Map();
  for (const item of items) {
    if (item.status !== "done") continue;
    for (const [index] of item.images?.entries() || []) {
      const time = Date.parse(item.started_at) + (item.frame_offsets?.[index] || 0) * 1000;
      const bucket = Math.floor(time / step) * step;
      if (!cells.has(bucket)) continue;
      for (const pet of Object.keys(PETS)) {
        const decision = item.pets[pet];
        if (decision?.visibility !== "present" || decision.activity === "not_visible" || decision.proposed_activity === "not_visible") continue;
        let key = activityKey(item, pet), priority = activityRank(key);
        for (const event of item.events || []) {
          if (event.pet !== pet && event.pet !== "both") continue;
          const rank = activityRank(event.type);
          if (rank < priority) { key = event.type; priority = rank; }
        }
        const id = pet + "-" + bucket, previous = photos.get(id);
        if (!previous || priority < previous.priority || priority === previous.priority && (time > previous.time || time === previous.time && item.id > previous.item.id)) {
          photos.set(id, {id, pet, item, index, time, key, priority, top: cells.get(bucket)});
        }
      }
    }
  }
  return [...photos.values()].sort((a, b) => a.top - b.top || a.pet.localeCompare(b.pet));
}

export function timelineLayout(episodes, start, end, compact = false, step = 15 * 60000) {
  if (!(end > start) || !Number.isFinite(step) || step <= 0) return {height: 160, ticks: [], intervals: [], clusters: []};
  const padding = 40, cellHeight = 120, scale = cellHeight / step;
  const axisStart = Math.floor(start / step) * step, axisEnd = Math.ceil(end / step) * step;
  const position = time => padding + (axisEnd - time) * scale;
  const ticks = [];
  for (let time = axisEnd; time >= axisStart; time -= step) ticks.push({time, top: position(time)});
  const lanes = {dog: [], cat: []}, intervals = [], groups = new Map(), clusters = [];
  for (const episode of [...episodes].sort((a, b) => b.end - a.end || a.start - b.start)) {
    const clippedStart = Math.max(start, episode.start), clippedEnd = Math.min(end, episode.end);
    if (clippedEnd <= clippedStart) continue;
    const top = position(clippedEnd), height = (clippedEnd - clippedStart) * scale;
    const boundaries = lanes[episode.pet];
    let lane = boundaries.findIndex(time => time >= clippedEnd);
    if (lane < 0) lane = boundaries.length;
    boundaries[lane] = clippedStart;
    const interval = {episode, top, height, lane, labelTop: null};
    intervals.push(interval);
    const middle = (clippedStart + clippedEnd) / 2;
    const bucket = Math.floor(middle / step) * step, id = episode.pet + "-" + bucket;
    if (!groups.has(id)) groups.set(id, {id, pet: episode.pet, start: bucket, end: bucket + step, intervals: []});
    groups.get(id).intervals.push(interval);
  }
  for (const group of groups.values()) {
    group.intervals.sort((a, b) => a.top + a.height / 2 - b.top - b.height / 2);
    const capacity = compact ? 2 : 3;
    const crowded = group.intervals.length > capacity;
    const shown = crowded
      ? [...group.intervals]
          .sort((a, b) => activityRank(a.episode.key) - activityRank(b.episode.key) || (a.top + a.height / 2) - (b.top + b.height / 2))
          .slice(0, capacity - 1)
          .sort((a, b) => (a.top + a.height / 2) - (b.top + b.height / 2))
      : group.intervals;
    const count = shown.length + Number(crowded);
    const first = position(group.end) + (compact ? 70 : 18), last = position(group.start) - 18;
    let cursor = first;
    for (const [index, interval] of shown.entries()) {
      interval.labelTop = Math.max(cursor, Math.min(interval.top + interval.height / 2, last - (count - index - 1) * 28));
      cursor = interval.labelTop + 28;
    }
    if (crowded) clusters.push({...group, top: last, hidden: group.intervals.length - shown.length});
  }
  return {height: position(axisStart) + padding, step, ticks, intervals, clusters};
}

export function durationLabel(milliseconds) {
  if (milliseconds < 60000) return "片刻";
  const minutes = Math.round(milliseconds / 60000);
  if (minutes < 60) return "约 " + minutes + " 分钟";
  const hours = Math.floor(minutes / 60), remainder = minutes % 60;
  return "约 " + hours + " 小时" + (remainder ? " " + remainder + " 分钟" : "");
}
