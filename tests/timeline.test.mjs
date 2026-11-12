import test from "node:test";
import assert from "node:assert/strict";
import {activityCandidate, activityKey, durationLabel, groupActivities, timelineLayout, timelinePhotos} from "../million_pound/web/timeline.js";
import {petNames} from "../million_pound/web/client.js";

function sample(id, start, dog = "sleeping", cat = "walking", status = "done") {
  const base = Date.parse("2026-11-11T04:00:00Z");
  return {id, started_at: new Date(base + start * 1000).toISOString(),
    ended_at: new Date(base + (start + 10) * 1000).toISOString(), status,
    pets: {dog: {activity: dog, confidence: .9, visibility: "present", identity_verified: true},
      cat: {activity: cat, confidence: .8, visibility: "present", identity_verified: true}}};
}

test("连续观察按宠物独立合并，并共享同一条时间轴", () => {
  const episodes = groupActivities([sample(3, 20, "sleeping", "playing"), sample(1, 0), sample(2, 10)]);
  const dog = episodes.filter(item => item.pet === "dog");
  const cat = episodes.filter(item => item.pet === "cat");
  assert.equal(dog.length, 1);
  assert.equal(dog[0].records.length, 3);
  assert.equal(cat.length, 2);
  const layout = timelineLayout(episodes, dog[0].start, dog[0].end);
  const dogInterval = layout.intervals.find(item => item.episode === dog[0]);
  const catInterval = layout.intervals.find(item => item.episode.key === "playing");
  assert.equal(layout.intervals.length, 3);
  assert.equal(dogInterval.top, catInterval.top);
  assert.equal(dogInterval.height, catInterval.height * 3);
});

test("时间区间共用线性坐标，时长、空档和跨侧重叠按真实时间保留", () => {
  const base = Date.parse("2026-11-11T04:00:00Z"), minute = 60000;
  const episodes = [
    {id: "dog-1", pet: "dog", start: base, end: base + 60 * minute},
    {id: "cat-1", pet: "cat", start: base + 30 * minute, end: base + 60 * minute},
    {id: "cat-2", pet: "cat", start: base + 70 * minute, end: base + 80 * minute},
  ];
  const layout = timelineLayout(episodes, base, base + 90 * minute);
  const interval = id => layout.intervals.find(item => item.episode.id === id);
  const dog = interval("dog-1"), cat = interval("cat-1"), later = interval("cat-2");
  assert.equal(dog.top, cat.top);
  assert.equal(dog.height, cat.height * 2);
  assert.equal(cat.top + cat.height, dog.top + dog.height / 2);
  assert.equal(cat.top - later.top - later.height, later.height);
  assert.equal(layout.ticks[0].time, base + 90 * minute);
  assert.equal(layout.ticks.at(-1).time, base);
  assert.equal(new Set(layout.ticks.map(item => item.time)).size, layout.ticks.length);
});

test("同侧重叠和相同结束时间的活动独立保留，连续区间复用括号位置", () => {
  const episodes = [
    {id: "a", pet: "dog", start: 20000, end: 60000},
    {id: "b", pet: "dog", start: 40000, end: 60000},
    {id: "c", pet: "dog", start: 0, end: 20000},
    {id: "d", pet: "cat", start: 20000, end: 60000},
  ];
  const layout = timelineLayout(episodes, 0, 60000);
  const interval = id => layout.intervals.find(item => item.episode.id === id);
  assert.equal(layout.intervals.length, episodes.length);
  assert.notEqual(interval("a").lane, interval("b").lane);
  assert.equal(interval("a").lane, interval("c").lane);
  assert.equal(interval("a").top + interval("a").height, interval("c").top);
  assert.equal(interval("a").top, interval("d").top);
  assert.equal(interval("a").height, interval("d").height);
  assert.equal(interval("d").lane, 0);
});

test("四种每格尺度保持真实区间比例和两侧对齐", () => {
  const start = Date.parse("2026-11-11T04:00:00Z");
  const episodes = [{pet: "dog", start, end: start + 60 * 60000}, {pet: "cat", start: start + 30 * 60000, end: start + 60 * 60000}];
  for (const minutes of [5, 15, 30, 60]) {
    for (const compact of [false, true]) {
      const step = minutes * 60000, layout = timelineLayout(episodes, start, start + 61 * 60000, compact, step);
      assert.equal(layout.step, step);
      assert.ok(layout.ticks.slice(1).every((tick, index) => layout.ticks[index].time - tick.time === step && tick.top - layout.ticks[index].top === 120));
      const dog = layout.intervals.find(item => item.episode.pet === "dog"), cat = layout.intervals.find(item => item.episode.pet === "cat");
      assert.equal(dog.height, 60 / minutes * 120);
      assert.equal(dog.height, cat.height * 2);
      assert.equal(dog.top, cat.top);
      assert.equal(dog.episode, episodes[0]);
    }
  }
  for (const invalid of [0, -1, NaN, Infinity]) assert.deepEqual(timelineLayout(episodes, start, start + 60000, false, invalid).ticks, []);
});

test("截图归格与拥挤折叠跟随所选尺度，跨格边界不借图", () => {
  for (const minutes of [5, 15, 30, 60]) {
    const crossing = sample(1, minutes * 60 - 5, "sleeping", "not_visible"), priority = sample(2, 0, "eating", "not_visible");
    crossing.images = ["early.jpg", "late.jpg"]; crossing.frame_offsets = [0, 5];
    priority.images = ["eating.jpg"]; priority.frame_offsets = [1];
    const start = Date.parse(priority.started_at), step = minutes * 60000;
    const episodes = Array.from({length: 18}, (_, index) => ({pet: "dog", start: start + index * 10500, end: start + (index + 1) * 10500}));
    for (const compact of [false, true]) {
      const layout = timelineLayout(episodes, start, start + step * 2, compact, step);
      const photos = timelinePhotos([crossing, priority], layout.ticks);
      assert.equal(photos.length, 2);
      assert.equal(photos[0].item, crossing);
      assert.equal(photos[0].index, 1);
      assert.equal(photos[0].time, start + step);
      assert.equal(photos[1].item, priority);
      assert.ok(photos.every(photo => photo.pet === "dog"));
      assert.equal(layout.clusters.length, 1);
      assert.equal(layout.clusters[0].start, start);
      assert.equal(layout.clusters[0].end, start + step);
      assert.equal(layout.clusters[0].intervals.length, 18);
      assert.equal(layout.clusters[0].hidden, compact ? 17 : 16);
    }
  }
});

test("密集短活动不撑高十五分钟网格，折叠名称仍保留每段真实区间", () => {
  const episodes = Array.from({length: 18}, (_, index) => ({pet: "dog", start: index * 10500, end: (index + 1) * 10500}));
  const layout = timelineLayout(episodes, 0, 18 * 10500);
  assert.equal(layout.step, 15 * 60000);
  assert.equal(layout.ticks.length, 2);
  assert.equal(layout.ticks[0].time - layout.ticks[1].time, 15 * 60000);
  assert.equal(layout.ticks[1].top - layout.ticks[0].top, 120);
  assert.equal(layout.intervals.length, 18);
  assert.ok(layout.intervals.every(item => Math.abs(item.height - 1.4) < 1e-6 && item.lane === 0));
  assert.equal(layout.intervals.filter(item => item.labelTop !== null).length, 2);
  assert.equal(layout.clusters.length, 1);
  assert.equal(layout.clusters[0].intervals.length, 18);
  assert.equal(layout.clusters[0].hidden, 16);
  const labelPositions = layout.intervals.filter(item => item.labelTop !== null).map(item => item.labelTop);
  labelPositions.push(layout.clusters[0].top);
  assert.ok(labelPositions[1] - labelPositions[0] >= 28);
  assert.ok(labelPositions[2] - labelPositions[1] >= 28);
  assert.ok(labelPositions.every(top => top > layout.ticks[0].top && top < layout.ticks[1].top));
  const long = {pet: "cat", start: 0, end: 18 * 10500};
  const both = timelineLayout([...episodes, long], 0, long.end);
  assert.equal(both.height, layout.height);
  assert.ok(Math.abs(both.intervals.find(item => item.episode === long).height - layout.intervals[0].height * 18) < 1e-6);
  assert.deepEqual(both.intervals.filter(item => item.episode.pet === "dog"), layout.intervals);
});

test("一格内的三段名称错开排列，跨格和两侧名称互不挤占", () => {
  const episodes = [
    {pet: "dog", start: 0, end: 10000}, {pet: "dog", start: 10000, end: 20000}, {pet: "dog", start: 20000, end: 30000},
    {pet: "cat", start: 0, end: 10000}, {pet: "dog", start: 900000, end: 910000},
  ];
  const layout = timelineLayout(episodes, 0, 1800000);
  assert.equal(layout.clusters.length, 0);
  const positions = layout.intervals.filter(item => item.episode.pet === "dog").map(item => item.labelTop);
  assert.ok(positions.every(top => Number.isFinite(top)));
  assert.ok(positions.slice(1).every((top, index) => top - positions[index] >= 28));
  assert.equal(layout.ticks[0].time, 1800000);
  assert.deepEqual(layout.ticks.map(item => item.time), [1800000, 900000, 0]);
});

test("只裁剪所选时段外的线段，保留原始活动与照片记录", () => {
  const episode = {pet: "dog", start: 0, end: 120000, records: [sample(1, 0)]};
  const outside = {pet: "cat", start: 0, end: 30000};
  const layout = timelineLayout([episode, outside], 30000, 90000);
  assert.equal(layout.intervals.length, 1);
  const interval = layout.intervals[0];
  const pixelsPerMinute = (layout.ticks[1].top - layout.ticks[0].top) / 15;
  assert.equal(interval.top, layout.ticks[0].top + 13.5 * pixelsPerMinute);
  assert.equal(interval.height, pixelsPerMinute);
  assert.equal(interval.episode, episode);
  assert.equal(episode.start, 0);
  assert.equal(episode.end, 120000);
  assert.equal(episode.records.length, 1);
});

test("空数据不生成无效坐标", () => {
  const layout = timelineLayout([], Infinity, -Infinity);
  assert.ok(Number.isFinite(layout.height));
  assert.deepEqual(layout.intervals, []);
  assert.deepEqual(layout.ticks, []);
  assert.deepEqual(timelinePhotos([], layout.ticks), []);
});

test("每格每只宠物只选本格内最近的确认入镜截图，按实际截图时间归格", () => {
  const first = sample(1, 0), later = sample(2, 30), crossing = sample(3, 890);
  first.images = later.images = ["frame.jpg"];
  first.frame_offsets = [5]; later.frame_offsets = [5];
  crossing.images = ["early.jpg", "late.jpg"];
  crossing.frame_offsets = [5, 10];
  crossing.pets.cat = {activity: "not_visible", visibility: "absent", identity_verified: false};
  const start = Date.parse(first.started_at), end = start + 30 * 60000;
  const layout = timelineLayout([], start, end);
  const photos = timelinePhotos([later, crossing, first], layout.ticks);
  assert.equal(photos.length, 3);
  const dog = photos.filter(photo => photo.pet === "dog");
  assert.equal(dog[0].item, crossing);
  assert.equal(dog[0].index, 1);
  assert.equal(dog[0].time, start + 15 * 60000);
  assert.equal(dog[0].top, (layout.ticks[0].top + layout.ticks[1].top) / 2);
  assert.equal(dog[1].item, crossing);
  assert.equal(dog[1].index, 0);
  const cat = photos.find(photo => photo.pet === "cat");
  assert.equal(cat.item, later);
  assert.equal(cat.top, dog[1].top);
  assert.equal(new Set(photos.map(photo => photo.id)).size, photos.length);
  assert.deepEqual(timelinePhotos([crossing, first, later], layout.ticks), photos);
});

test("没有入镜、没有截图、判断未完成或截图在筛选外时不放图，不借用相邻格的照片", () => {
  const missing = sample(1, 0), absent = sample(2, 10, "not_visible", "not_visible");
  const unverified = sample(3, 20), pending = sample(4, 30, "sleeping", "walking", "pending");
  const conflicting = sample(5, 40), outside = sample(6, -10);
  for (const item of [absent, unverified, pending, conflicting, outside]) { item.images = ["frame.jpg"]; item.frame_offsets = [0]; }
  for (const decision of Object.values(unverified.pets)) decision.visibility = "uncertain";
  for (const decision of Object.values(conflicting.pets)) decision.proposed_activity = "not_visible";
  const start = Date.parse(missing.started_at);
  const layout = timelineLayout([], start, start + 30 * 60000);
  assert.deepEqual(timelinePhotos([missing, absent, unverified, pending, conflicting, outside], layout.ticks), []);
  const visible = sample(7, 0, "sleeping", "not_visible"); visible.images = ["frame.jpg"]; visible.frame_offsets = [0];
  const photos = timelinePhotos([visible], layout.ticks);
  assert.equal(photos.length, 1);
  assert.equal(photos[0].pet, "dog");
  assert.equal(photos[0].top, (layout.ticks[1].top + layout.ticks[2].top) / 2);
});

test("已确认入镜但活动未确认时仍可显示画面，不生成未知活动", () => {
  const item = sample(1, 0, "uncertain", "not_visible"); item.images = ["frame.jpg"]; item.frame_offsets = [5];
  const start = Date.parse(item.started_at), layout = timelineLayout([], start, start + 15 * 60000);
  assert.equal(activityKey(item, "dog"), null);
  assert.equal(timelinePhotos([item], layout.ticks).length, 1);
});

test("手机布局给图片留出上半格，活动折叠不改变真实时间坐标", () => {
  const episodes = Array.from({length: 3}, (_, index) => ({pet: "dog", start: index * 10000, end: (index + 1) * 10000}));
  const desktop = timelineLayout(episodes, 0, 900000), mobile = timelineLayout(episodes, 0, 900000, true);
  assert.deepEqual(mobile.ticks, desktop.ticks);
  assert.deepEqual(mobile.intervals.map(({top, height}) => ({top, height})), desktop.intervals.map(({top, height}) => ({top, height})));
  assert.equal(mobile.clusters.length, 1);
  assert.equal(mobile.clusters[0].hidden, 2);
  const labels = mobile.intervals.filter(interval => interval.labelTop !== null);
  assert.equal(labels.length, 1);
  assert.ok(labels[0].labelTop - mobile.ticks[0].top >= 70);
  assert.ok(mobile.clusters[0].top - labels[0].labelTop >= 28);
});

test("同格图片严格按乱拉乱尿、拆家、吃喝、玩耍、走动、睡觉排序，不被较新低优先级覆盖", () => {
  const order = ["toileting", "destruction", "eating", "playing", "walking", "sleeping", "other"];
  const items = order.map((key, index) => {
    const item = sample(index + 1, index * 60, index < 2 ? "sleeping" : key, "not_visible");
    item.images = ["frame.jpg"]; item.frame_offsets = [5];
    if (index < 2) item.events = [{type: key, pet: "dog"}];
    return item;
  });
  const start = Date.parse(items[0].started_at), layout = timelineLayout([], start, start + 15 * 60000);
  for (let index = 0; index < order.length; index++) {
    const [photo] = timelinePhotos(items.slice(index), layout.ticks);
    assert.equal(photo.item.id, index + 1);
    assert.equal(photo.key, order[index]);
  }
  assert.deepEqual(timelinePhotos([...items].reverse(), layout.ticks), timelinePhotos(items, layout.ticks));
});

test("预警优先级只作用于对应入镜宠物，相同事件仍选择较新截图", () => {
  const first = sample(1, 0), later = sample(2, 30, "walking", "walking");
  for (const item of [first, later]) { item.images = ["frame.jpg"]; item.frame_offsets = [5]; }
  first.events = [{type: "toileting", pet: "cat"}, {type: "destruction", pet: "unknown"}];
  const start = Date.parse(first.started_at), layout = timelineLayout([], start, start + 15 * 60000);
  let photos = timelinePhotos([first, later], layout.ticks);
  assert.equal(photos.find(photo => photo.pet === "cat").item, first);
  assert.equal(photos.find(photo => photo.pet === "dog").item, later);
  later.events = [{type: "toileting", pet: "both"}];
  photos = timelinePhotos([first, later], layout.ticks);
  assert.ok(photos.every(photo => photo.item === later && photo.key === "toileting"));
  later.pets.dog.visibility = "uncertain";
  assert.equal(timelinePhotos([later], layout.ticks).length, 1);
});

test("旧吃饭和喝水记录合并为吃喝，保留原始记录", () => {
  const first = sample(1, 0, "drinking");
  const episodes = groupActivities([first, sample(2, 10, "eating")]);
  const dog = episodes.find(item => item.pet === "dog");
  assert.equal(activityKey(first, "dog"), "eating");
  assert.equal(dog.records.length, 2);
  assert.equal(first.pets.dog.activity, "drinking");
});

test("后端延续的低分活动保持合并，切换活动时正常分段并保留原始分数", () => {
  const first = sample(1, 0), continued = sample(2, 10), changed = sample(3, 20, "walking");
  continued.pets.dog = {...continued.pets.dog, confidence: .01, proposed_activity: "sleeping", continued: true};
  const dog = groupActivities([changed, first, continued]).filter(item => item.pet === "dog");
  assert.deepEqual(dog.map(item => item.key), ["walking", "sleeping"]);
  assert.equal(dog[1].records.length, 2);
  assert.equal(continued.pets.dog.confidence, .01);
  assert.equal(continued.pets.dog.continued, true);
});

test("未判断、未入镜和采集空档会断开活动，不用睡觉线段填补隐藏时段", () => {
  const interrupted = groupActivities([sample(1, 0), sample(2, 10, "sleeping", "walking", "error"), sample(3, 20)]);
  const merged = interrupted.filter(item => item.pet === "dog");
  assert.equal(merged.length, 2);
  assert.equal(merged[0].key, "sleeping");
  assert.equal(merged[0].records.length, 1);
  assert.equal(groupActivities([sample(1, 0), sample(2, 120)]).filter(item => item.pet === "dog").length, 2);
  const pending = groupActivities([sample(1, 0), sample(2, 10, "sleeping", "walking", "pending"), sample(3, 20)]);
  assert.equal(pending.filter(item => item.pet === "dog").length, 2);
});

test("未入镜、没有候选的旧不确定记录和失败时段隐藏，不产生未知活动", () => {
  const absent = sample(1, 0, "not_visible"), unclear = sample(2, 10, "uncertain"), legacy = sample(3, 20);
  delete legacy.pets.dog.visibility;
  const items = [absent, unclear, legacy, sample(4, 30, "sleeping", "walking", "error"), sample(5, 40, "walking")];
  const episodes = groupActivities(items).filter(item => item.pet === "dog");
  assert.deepEqual(episodes.map(item => item.key), ["walking"]);
  assert.deepEqual(episodes[0].records, [items[4]]);
  assert.equal(activityKey(absent, "dog"), null);
  assert.equal(absent.pets.dog.activity, "not_visible");
  assert.equal(unclear.pets.dog.activity, "uncertain");
  assert.equal(legacy.pets.dog.activity, "sleeping");
  const lowAbsent = sample(6, 50, "uncertain");
  lowAbsent.pets.dog.visibility = "absent";
  lowAbsent.pets.dog.identity_verified = false;
  assert.equal(activityKey(lowAbsent, "dog"), null);
  assert.equal(groupActivities([absent, sample(7, 20, "uncertain")]).filter(item => item.pet === "dog").length, 0);
  const missing = sample(8, 60);
  delete missing.pets.dog;
  assert.equal(activityKey(missing, "dog"), null);
});

test("其他活动仍会被未入镜时段断开", () => {
  const before = sample(1, 0, "walking"), absent = sample(2, 10, "walking"), after = sample(3, 20, "walking");
  absent.pets.dog = {activity: "not_visible", confidence: .05, visibility: "absent", visibility_confidence: .9, identity_verified: false};
  const episodes = groupActivities([before, absent, after]).filter(item => item.pet === "dog");
  assert.deepEqual(episodes.map(item => item.key), ["walking", "walking"]);
  assert.deepEqual(episodes.map(item => item.records.length), [1, 1]);
});

test("未达到确认分数的活动均显示疑似，睡觉休息与吃饭喝水使用统一名称", () => {
  const sleeping = sample(1, 0, "uncertain");
  sleeping.pets.dog = {...sleeping.pets.dog, confidence: .32, proposed_activity: "sleeping"};
  assert.equal(activityKey(sleeping, "dog"), "suspect:sleeping");
  const resting = sample(2, 10, "uncertain");
  resting.pets.dog = {...resting.pets.dog, confidence: .42, proposed_activity: "resting"};
  assert.equal(activityKey(resting, "dog"), "suspect:sleeping");
  const drinking = sample(3, 20, "uncertain");
  drinking.pets.dog = {...drinking.pets.dog, confidence: .4, proposed_activity: "drinking"};
  assert.equal(activityKey(drinking, "dog"), "suspect:eating");
  const walking = sample(4, 30, "uncertain");
  walking.pets.dog = {...walking.pets.dog, confidence: .4, proposed_activity: "walking"};
  assert.equal(activityKey(walking, "dog"), "suspect:walking");
  const unknown = sample(5, 40, "uncertain");
  unknown.pets.dog = {...unknown.pets.dog, confidence: .3, proposed_activity: "not_visible"};
  assert.equal(activityKey(unknown, "dog"), null);
  const unverified = sample(6, 50, "uncertain");
  unverified.pets.dog = {...unverified.pets.dog, identity_verified: false, proposed_activity: "playing"};
  assert.equal(activityKey(unverified, "dog"), "suspect:playing");
});

test("连续睡觉与疑似睡觉有确认即合并，但不跨越未入镜和中断时段", () => {
  const confirmed = sample(1, 0, "sleeping"), resting = sample(2, 10, "resting");
  const sleep = sample(3, 20, "uncertain"), rest = sample(4, 50, "uncertain");
  for (const item of [sleep, rest]) item.pets.dog = {...item.pets.dog, activity: "uncertain", confidence: .4,
    proposed_activity: item === sleep ? "sleeping" : "resting"};
  const cancelled = sample(5, 30, "sleeping", "walking", "cancelled"), absent = sample(6, 40, "not_visible");
  absent.pets.dog = {...absent.pets.dog, activity: "not_visible", visibility: "absent", identity_verified: false};
  const resume = sample(7, 60, "sleeping");
  const dog = groupActivities([confirmed, resting, sleep, cancelled, absent, rest, resume]).filter(item => item.pet === "dog");
  assert.deepEqual(dog.map(item => item.key), ["sleeping", "sleeping"]);
  assert.equal(dog.at(-1).records.length, 3);
  assert.equal(dog.at(-1).end - dog.at(-1).start, 30000);
  assert.equal(dog[0].records.length, 2);
});

test("模型输出不确定时，取最高分的真实活动作为疑似，保留原始不确定分数", () => {
  const item = sample(1, 0, "uncertain");
  item.pets.dog = {...item.pets.dog, confidence: .8, proposed_activity: "uncertain",
    probabilities: {uncertain: .8, not_visible: .5, playing: .18, sleeping: .07, walking: .25}};
  assert.equal(activityCandidate(item.pets.dog), "walking");
  assert.equal(activityKey(item, "dog"), "suspect:walking");
  assert.equal(item.pets.dog.confidence, .8);
  assert.equal(item.pets.dog.proposed_activity, "uncertain");
  const absent = structuredClone(item);
  absent.pets.dog.visibility = "absent";
  assert.equal(activityKey(absent, "dog"), null);
  absent.pets.dog.visibility = "present";
  absent.pets.dog.proposed_activity = "not_visible";
  assert.equal(activityKey(absent, "dog"), null);
});

test("候选只接受有效的活动分数，平分时保持原始排序，无候选时隐藏", () => {
  const item = sample(1, 0, "uncertain");
  item.pets.dog = {...item.pets.dog, proposed_activity: "uncertain", probabilities: {
    uncertain: .9, not_visible: .8, unknown: .7, walking: NaN, eating: true, sleeping: -1, resting: Infinity, playing: .2, other: .2}};
  assert.equal(activityCandidate(item.pets.dog), "playing");
  assert.equal(activityKey(item, "dog"), "suspect:playing");
  item.pets.dog.probabilities = {uncertain: .9, not_visible: .1};
  assert.equal(activityKey(item, "dog"), null);
});

test("所有活动的确认与疑似按同一状态合并，原始判断仍保留", () => {
  const confirmed = sample(1, 0, "walking");
  const suspected = sample(2, 10, "uncertain"), later = sample(3, 20, "uncertain");
  for (const item of [suspected, later]) item.pets.dog = {...item.pets.dog, activity: "uncertain", confidence: .35, proposed_activity: "walking"};
  const dog = groupActivities([confirmed, suspected, later]).filter(item => item.pet === "dog");
  assert.deepEqual(dog.map(item => item.key), ["walking"]);
  assert.equal(dog[0].records.length, 3);
  assert.equal(dog[0].records[1].pets.dog.activity, "uncertain");
});

test("全为疑似时保留疑似，任一位置有确认则整段同类状态稳定，真正换状态仍切段", () => {
  for (const activity of ["sleeping", "walking", "eating", "playing", "other"]) {
    const items = [sample(1, 0, "uncertain"), sample(2, 10, "uncertain"), sample(3, 20, "uncertain")];
    for (const item of items) item.pets.dog = {...item.pets.dog, proposed_activity: activity, confidence: .2};
    assert.equal(groupActivities(items).find(item => item.pet === "dog").key, "suspect:" + activity);
    for (let index = 0; index < items.length; index++) {
      const withConfirmation = structuredClone(items);
      withConfirmation[index].pets.dog.activity = activity;
      const dog = groupActivities(withConfirmation).filter(item => item.pet === "dog");
      assert.equal(dog.length, 1);
      assert.equal(dog[0].key, activity);
      assert.equal(dog[0].records.length, 3);
    }
    const changed = sample(4, 30, activity === "walking" ? "playing" : "walking");
    assert.equal(groupActivities([...items, changed]).filter(item => item.pet === "dog").length, 2);
  }
});

test("隐藏的未入镜时段不生成时间线节点", () => {
  const absent = sample(1, 0, "not_visible", "walking");
  absent.pets.dog = {...absent.pets.dog, activity: "not_visible", visibility: "absent", identity_verified: false};
  const visible = sample(2, 10, "sleeping", "playing");
  const layout = timelineLayout(groupActivities([absent, visible]), Date.parse(absent.started_at), Date.parse(visible.ended_at));
  assert.equal(layout.intervals.length, 3);
  assert.equal(layout.intervals.filter(item => item.episode.pet === "dog").length, 1);
  assert.equal(layout.intervals.find(item => item.episode.pet === "dog").episode.key, "sleeping");
  assert.deepEqual(layout.intervals.filter(item => item.episode.pet === "cat").map(item => item.episode.key), ["playing", "walking"]);
});

test("缺少入镜判断时隐藏，已入镜不再受旧身份标记和分数门槛限制", () => {
  const legacy = sample(1, 0);
  delete legacy.pets.cat.visibility;
  assert.equal(activityKey(legacy, "cat"), null);
  assert.equal(legacy.pets.cat.activity, "walking");
  legacy.pets.cat.activity = "sleeping";
  assert.equal(activityKey(legacy, "cat"), null);
  const current = sample(2, 10, "sleeping", "sleeping");
  current.pets.cat.identity_verified = false;
  current.pets.cat.visibility_confidence = .1;
  assert.equal(activityKey(current, "cat"), "sleeping");
  current.images = ["frame.jpg"]; current.frame_offsets = [5];
  const start = Date.parse(current.started_at), layout = timelineLayout([], start, start + 900000);
  assert.equal(timelinePhotos([current], layout.ticks).filter(photo => photo.pet === "cat").length, 1);
});

test("旧版入镜分数拦下的短缺口连接两侧睡觉，保留原始缺口观察", () => {
  const before = sample(1, 0), gap = sample(2, 10, "uncertain"), secondGap = sample(3, 20, "uncertain"), after = sample(4, 30);
  for (const item of [gap, secondGap]) item.pets.dog = {
    activity: "uncertain", confidence: 0, visibility: "present", visibility_confidence: .75, identity_verified: false};
  const original = structuredClone([before, gap, secondGap, after]);
  const dog = groupActivities([after, gap, before, secondGap]).filter(episode => episode.pet === "dog");
  assert.equal(dog.length, 1);
  assert.equal(dog[0].key, "sleeping");
  assert.equal(dog[0].end - dog[0].start, 40000);
  assert.deepEqual(dog[0].records, [before, gap, secondGap, after]);
  assert.deepEqual([before, gap, secondGap, after], original);
  assert.equal(activityKey(gap, "dog"), null);
});

test("睡觉缺口仅在旧版漏存活动且前后睡觉、时间连续并不超过一分钟时连接", () => {
  const before = sample(1, 0), gap = sample(2, 10, "uncertain"), after = sample(3, 20);
  gap.pets.dog = {activity: "uncertain", confidence: 0, visibility: "present", identity_verified: false};
  for (const visibility of ["absent", "uncertain", undefined]) {
    const hidden = structuredClone(gap); hidden.pets.dog.visibility = visibility;
    assert.equal(groupActivities([before, hidden, after]).filter(episode => episode.pet === "dog").length, 2);
  }
  for (const status of ["error", "cancelled", "skipped", "pending"]) {
    assert.equal(groupActivities([before, {...gap, status}, after]).filter(episode => episode.pet === "dog").length, 2);
  }
  const uncertain = structuredClone(gap); uncertain.pets.dog.proposed_activity = "uncertain";
  assert.equal(groupActivities([before, uncertain, after]).filter(episode => episode.pet === "dog").length, 2);
  const changed = structuredClone(after); changed.pets.dog.activity = "walking";
  assert.deepEqual(groupActivities([before, gap, changed]).filter(episode => episode.pet === "dog").map(episode => episode.key), ["walking", "sleeping"]);
  const trailing = groupActivities([before, gap]).find(episode => episode.pet === "dog");
  assert.equal(trailing.end, Date.parse(before.ended_at));
  assert.deepEqual(trailing.records, [before]);
  const gaps = Array.from({length: 7}, (_, i) => ({...structuredClone(gap), id: i + 2,
    started_at: new Date(Date.parse(gap.started_at) + i * 10000).toISOString(),
    ended_at: new Date(Date.parse(gap.ended_at) + i * 10000).toISOString()}));
  assert.equal(groupActivities([before, ...gaps.slice(0, 6), sample(9, 70)]).filter(episode => episode.pet === "dog").length, 1);
  assert.equal(groupActivities([before, ...gaps, sample(10, 80)]).filter(episode => episode.pet === "dog").length, 2);
  const discontinuous = structuredClone(gap);
  discontinuous.started_at = new Date(Date.parse(gap.started_at) + 5000).toISOString();
  assert.equal(groupActivities([before, discontinuous, after]).filter(episode => episode.pet === "dog").length, 2);
});

test("名字与时长使用简洁的显示方式", () => {
  assert.equal(petNames("拉布拉多犬和缅因猫在猫砂盆旁"), "百万和英镑在猫砂盆旁");
  assert.equal(durationLabel(10000), "片刻");
  assert.equal(durationLabel(120000), "约 2 分钟");
  assert.equal(durationLabel(3660000), "约 1 小时 1 分钟");
});
