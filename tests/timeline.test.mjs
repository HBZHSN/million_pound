import test from "node:test";
import assert from "node:assert/strict";
import {activityKey, durationLabel, groupActivities, timelineMoments} from "../million_pound/web/timeline.js";
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
  const moments = timelineMoments(episodes);
  assert.equal(moments.length, 2);
  assert.equal(moments[0].dog, dog[0]);
  assert.equal(moments[0].cat.key, "playing");
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

test("中断、未确认与未采集时段不补猜成连续活动", () => {
  assert.equal(groupActivities([sample(1, 0), sample(2, 20)]).filter(item => item.pet === "dog").length, 2);
  const episodes = groupActivities([sample(1, 0), sample(2, 10, "sleeping", "walking", "error"), sample(3, 20)]);
  assert.deepEqual(episodes.filter(item => item.pet === "dog").map(item => item.key), ["sleeping", "unknown", "sleeping"]);
  const pending = groupActivities([sample(1, 0), sample(2, 10, "sleeping", "walking", "pending"), sample(3, 20)]);
  assert.equal(pending.filter(item => item.pet === "dog").length, 2);
});

test("未入镜、判断不清、旧判断待核验和失败连续合并为未知，保留原始记录", () => {
  const absent = sample(1, 0, "not_visible"), unclear = sample(2, 10, "uncertain"), legacy = sample(3, 20);
  delete legacy.pets.dog.visibility;
  const items = [absent, unclear, legacy, sample(4, 30, "sleeping", "walking", "error"), sample(5, 40, "walking")];
  const episodes = groupActivities(items).filter(item => item.pet === "dog");
  assert.deepEqual(episodes.map(item => item.key), ["walking", "unknown"]);
  assert.deepEqual(episodes[1].records, items.slice(0, 4));
  assert.equal(episodes[1].end - episodes[1].start, 40000);
  assert.equal(absent.pets.dog.activity, "not_visible");
  assert.equal(unclear.pets.dog.activity, "uncertain");
  assert.equal(legacy.pets.dog.activity, "sleeping");
  const missing = sample(6, 50);
  delete missing.pets.dog;
  assert.equal(activityKey(missing, "dog"), "unknown");
  assert.equal(groupActivities([absent, sample(7, 20, "uncertain")]).filter(item => item.pet === "dog").length, 2);
});

test("旧睡觉与缺少入镜确认的睡觉显示未知，已确认活动正常显示", () => {
  const legacy = sample(1, 0);
  delete legacy.pets.cat.visibility;
  assert.equal(activityKey(legacy, "cat"), "walking");
  assert.equal(legacy.pets.cat.activity, "walking");
  legacy.pets.cat.activity = "sleeping";
  assert.equal(activityKey(legacy, "cat"), "unknown");
  const current = sample(2, 10, "sleeping", "sleeping");
  current.pets.cat.identity_verified = false;
  assert.equal(activityKey(current, "cat"), "unknown");
  current.pets.cat.identity_verified = true;
  assert.equal(activityKey(current, "cat"), "sleeping");
});

test("名字与时长使用简洁的显示方式", () => {
  assert.equal(petNames("拉布拉多犬和缅因猫在猫砂盆旁"), "百万和英镑在猫砂盆旁");
  assert.equal(durationLabel(10000), "片刻");
  assert.equal(durationLabel(120000), "约 2 分钟");
  assert.equal(durationLabel(3660000), "约 1 小时 1 分钟");
});
