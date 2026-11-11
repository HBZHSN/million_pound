import test from "node:test";
import assert from "node:assert/strict";
import {activityKey, durationLabel, groupActivities, timelineMoments} from "../million_pound/web/timeline.js";
import {petNames} from "../million_pound/web/client.js";

function sample(id, start, dog = "sleeping", cat = "walking", status = "done") {
  const base = Date.parse("2026-11-11T04:00:00Z");
  return {id, started_at: new Date(base + start * 1000).toISOString(),
    ended_at: new Date(base + (start + 10) * 1000).toISOString(), status,
    pets: {dog: {activity: dog, confidence: .9}, cat: {activity: cat, confidence: .8}}};
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

test("中断、未确认与未采集时段不补猜成连续活动", () => {
  assert.equal(groupActivities([sample(1, 0), sample(2, 20)]).filter(item => item.pet === "dog").length, 2);
  const episodes = groupActivities([sample(1, 0), sample(2, 10, "sleeping", "walking", "error"), sample(3, 20)]);
  assert.deepEqual(episodes.filter(item => item.pet === "dog").map(item => item.key), ["sleeping", "gap", "sleeping"]);
  const pending = groupActivities([sample(1, 0), sample(2, 10, "sleeping", "walking", "pending"), sample(3, 20)]);
  assert.equal(pending.filter(item => item.pet === "dog").length, 2);
});

test("未入镜与判断不清分别保留", () => {
  const episodes = groupActivities([sample(1, 0, "not_visible"), sample(2, 10, "uncertain")]);
  assert.deepEqual(episodes.filter(item => item.pet === "dog").map(item => item.key), ["uncertain", "not_visible"]);
});

test("名字与时长使用简洁的显示方式", () => {
  assert.equal(petNames("拉布拉多犬和缅因猫在猫砂盆旁"), "百万和英镑在猫砂盆旁");
  assert.equal(durationLabel(10000), "片刻");
  assert.equal(durationLabel(120000), "约 2 分钟");
  assert.equal(durationLabel(3660000), "约 1 小时 1 分钟");
});
