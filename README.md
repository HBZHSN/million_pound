# Million Pound · 家庭宠物守护

面向一只拉布拉多和一只缅因猫，识别吃饭、拆家、乱拉乱尿。Python + FastAPI + FFmpeg，配置网页与监控任务由同一服务运行，事件存入 SQLite。

## 在 Python 虚拟环境中启动

需要 Python 3.11+、FFmpeg（含 ffprobe 和 libx264）。所有 Python 命令均使用项目的 `.venv`，不向系统 Python 安装依赖。

```bash
cd ~/million-pound
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m million_pound init
.venv/bin/python -m million_pound serve
```

访问 `http://127.0.0.1:8097`。`init` 只运行一次，会显示随机初始密码，磁盘只保存密码哈希。默认暂停监控；配置视频源、测试连接、设置通知方式后，点击“开启守护”。8099 已由摄像头桥接占用，应用使用 8097。

当前开发设备已初始化、配置好视频源、模型与 Bark，后台服务及 linger 均已启用。直接登录即可使用，无需再次运行 `init`。

修改登录密码：

```bash
.venv/bin/python -m million_pound password
```

按提示输入至少 12 位密码，重启应用后生效；旧登录状态和旧图片签名一起失效。Cookie 有效期 12 小时，HTTPS 访问时启用 Secure。

## 小米摄像头接入

采用 [ha-xiaomi-camera 独立桥接服务](https://github.com/willjayyyy/ha-xiaomi-camera/blob/main/README.zh-CN.md#不用-home-assistant-也能跑)。需要 Docker，桥接主机与摄像头处于同一局域网。

1. 首次部署时复制 `.env.example` 为 `.env`，填写两个不同的随机长密码，并执行 `chmod 600 .env`。当前开发设备已配置，无需重复覆盖。
2. 执行 `sudo docker compose -f compose.camera.yaml up -d`。
3. 在 PC 直接打开 `http://127.0.0.1:8098`，用 `.env` 中的 `XIAOMI_CAMERA_WEB_PASSWORD` 登录。
4. 在桥接页面连接小米账号；小米登录完成后，将浏览器完整回调地址按页面提示粘贴回去。
5. 将摄像头的视频流地址填入应用设置。桥接为局域网模式，RTSP 地址需加入用户名 `xiaomi` 与 `.env` 中的 `XIAOMI_CAMERA_RTSP_PASSWORD`，形式为 `rtsp://xiaomi:密码@127.0.0.1:8554/camera_设备ID`。
6. 保存设置，点击“测试并查看截图”。默认使用原始视频流，应用支持 H.264/H.265 输入。

小米授权持久化在 `camera-data/`。不要把 8098 或 8554 映射到公网域名；公网只代理 8097 的应用。桥接镜像版本采用上游 `latest`，升级后需重新测试取流。

## 监控行为

- 按北京时间执行每天的监控时段，支持跨午夜；不填时段或开始与结束相同表示全天。
- 监控开启后保持同一条视频连接持续录像，按片段交给独立的分析任务；模型处理和复核期间继续采集。默认每段十秒，每秒抽两帧、四倍速，对应约二十帧的加速视频。页面可调整片段长度、抽帧率和倍速，旧版的采样间隔已移除，旧配置会自动迁移。
- 原速录像编码为 H.264 并将首帧时间对齐到零，避免 RTSP 等待关键帧导致短片开头空白或无截图；录像保留原始分辨率。
- 加速视频交给 `d3` 的 `/v1/systemone`，同一次请求判断两只宠物的日常活动及三类预警。预警概率达到 0.7 进入候选；接口字段缺失或无效会明确标记失败，不当作“没有事件”或“未入镜”。
- 候选截图取自原速录像的 10%、50%、90% 位置；事件时间显示本段原视频采样开始时间，截图标注原视频内的秒数。
- 仅有候选且达到复核间隔时，才调用 `swift-1.5` 的 `/v1/chat/completions`。复核开始时间持久化，失败及重启也不会绕过最短间隔。
- 因间隔限制未复核的候选标为“未复核”，不排队、不发送通知；之后新采样仍有该行为时再复核。原片不长期保存。
- 只有复核明确确认的事件才按类型发送 Bark；默认吃饭只记录、拆家及乱拉乱尿通知，可在网页修改。
- 同类型、同宠物在五分钟内合并通知。通知失败会标明；发送中断时标为“结果待确认”，不会自动重试原通知。
- 保存配置或暂停守护会停止当前连续录像，未完成判断的记录会标记中断；结尾不足一段的片段会尽可能保留截图，不补猜活动或发送通知。
- 待分析原片最多缓存三十段或 256MiB；超出时仍保留该时段截图，明确标为“积压未判断”。磁盘可用空间不足 1GB 时暂停采集，避免占满磁盘。
- HTTPX 与 FFmpeg 不使用系统代理，保证局域网模型和摄像头直连。模型服务地址可填写根地址或带 `/v1` 的地址。

连续录像使用 [FFmpeg segment](https://ffmpeg.org/ffmpeg-formats.html#segment_002c-stream_005fsegment_002c-ssegment)，抽帧使用 [fps 过滤器](https://ffmpeg.org/ffmpeg-filters.html#fps-1)。抽帧与分段判断仍可能遗漏短暂行为，实际识别效果需用家中宠物画面调整参数，不保证零误报或漏报。

## 带图片的活动时间线

访问 `http://127.0.0.1:8097/activities`，也可点击首页导航“活动时间线”。使用原有登录密码。

- 每个录像时段各记录拉布拉多和缅因猫的主要活动：睡觉、喝水、吃饭、走动 / 散步、玩耍、休息、未入镜、其他活动或判断不清。
- d3 选择结果低于 0.6 时显示“判断不清”，不把低置信度当成确定活动；日常活动不调用 swift-1.5，也不直接触发通知。
- 每段在原视频中间位置保存一张截图，标明录像起止时间和截图时间；点击图片查看原图。预警复核需要时再补充前后两张截图。
- 网页时间按采集端估算，可能有几秒传输和编码延迟；截图同时带有摄像头自身时钟，可据此核对。
- 支持北京时间日期、小时和宠物筛选，从新到旧查看并加载更早记录；页面每五秒刷新，复用未变化的图片元素。
- “未入镜”表示模型判断宠物不在画面内；遮挡、身份不清、模型失败、分析积压和停止采集分别标记，不补填未录制时段。
- 图片与时间线接口均需登录，沿用截图保留期，默认七天。到期保留活动文字与时间记录，图片显示已过期。

## Bark 与公网截图

在配置页填写 Bark 地址。可以直接粘贴 Bark 示例中的完整 URL，应用会提取设备密钥并保留 `group`、`ttl` 等查询选项，再替换通知标题与正文。支持常规 `https://服务器/设备密钥/标题/正文` 格式。

按 [Bark 官方参数](https://github.com/Finb/Bark/blob/master/docs/en-us/params.md#image)，图片需要手机可访问的 URL：

1. 将 `https://pets.example.com` 的 HTTPS 反向代理指向 `127.0.0.1:8097`，参考 `deploy/nginx-location.conf`。转发 Host 和 X-Forwarded-Proto。
2. 在网页“公网访问地址”填写该 HTTPS 地址；留空时只发文字，事件里会明确标记。
3. 通知使用 `/shared/文件名?expires=...&sig=...`，一小时内无需登录即可读取指定截图；篡改文件名或到期会拒绝访问。普通事件与截图接口始终需要登录。
4. 点击“发送测试通知”可测试文字通道；真实确认事件用于最终验证手机图片展示。

## 后台运行

仓库提供当前设备的用户级 systemd 服务；换目录时修改其中的绝对路径。

```bash
mkdir -p ~/.config/systemd/user
cp deploy/million-pound.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now million-pound
systemctl --user status million-pound
tail -n 50 data/server.log
```

若需要退出 SSH 后继续运行及开机启动，由管理员执行一次 `sudo loginctl enable-linger "$USER"`。

只运行一个应用实例、一个 worker，避免重复采样与推送。更改本地配置文件或 CLI 修改密码后，执行 `systemctl --user restart million-pound`；网页修改设置即时生效。

## 本地数据与检查

- `config.json`：模型密钥、Bark 地址、视频源及设置，权限 600；网页不会返回已保存的密钥或带凭据的视频源。留空保留已有值，接口 PUT 显式传空字符串可清除。
- `.env`：桥接网页与 RTSP 密码，权限 600。
- `data/auth.json`：密码哈希和签名密钥。
- `data/events.sqlite3`：原有事件，以及新增的宠物活动时段、模型结果和截图索引；升级保留已有事件。
- `data/images/`：活动与事件截图，默认七天，约每小时检查清理；到期保留文字和时间记录。
- 原片分析结束即删除；停止采集时清理缓存，异常退出遗留的临时片段在下次启动时删除。
- 原始 `需求.txt` 含密钥，和以上运行数据均已通过 `.gitignore` 排除。

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试不调用真实模型、不发送通知，覆盖北京时间时段、配置迁移、连续切片与抽帧、模型等待时继续采集、队列积压、暂停清理、活动分页与图片保留、复核节流、通知去重、登录、CSRF、密钥脱敏及图片签名。

2026-11-11：首版 17 项检查通过，连续采集与活动时间线升级后共 24 项检查通过。另已实测摄像头连续切片、d3 联合活动判断、带截图的时间线，以及两个模型、Bark 文字发送和浏览器操作。真实确认事件的图文通知需在公网域名接通后继续验证。
