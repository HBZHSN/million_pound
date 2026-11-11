import asyncio
import csv
import json
import math
import os
import re
import signal
from pathlib import Path
from urllib.parse import urlsplit


class MediaError(RuntimeError):
    pass


async def command(*args: str, timeout: float):
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise MediaError("视频处理失败，请检查视频源、镜头开关和桥接服务")
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def record(source: str, target: Path, seconds: int):
    transport = (["-rtsp_transport", "tcp", "-timeout", "15000000"]
                 if urlsplit(source).scheme in {"rtsp", "rtsps"} else ["-rw_timeout", "15000000"])
    await command(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        *transport, "-i", source, "-t", str(seconds), "-map", "0:v:0", "-an",
        "-vf", "setpts=PTS-STARTPTS", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-fps_mode", "vfr",
        str(target), timeout=seconds + 30,
    )
    duration = await probe_duration(target)
    if not 0.1 <= duration <= seconds + 10:
        raise MediaError("录像时长异常")
    return duration


async def probe_duration(target: Path):
    output = await command("ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "json", str(target), timeout=10)
    try:
        duration = float(json.loads(output)["format"]["duration"])
        if not math.isfinite(duration) or duration < .1:
            raise ValueError
        return duration
    except (KeyError, TypeError, ValueError):
        raise MediaError("录像时长无效") from None


async def segments(source: str, directory: Path, seconds: int):
    transport = (["-rtsp_transport", "tcp", "-timeout", "15000000"]
                 if urlsplit(source).scheme in {"rtsp", "rtsps"} else ["-rw_timeout", "15000000"])
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", *transport, "-i", source,
        "-map", "0:v:0", "-an", "-vf", "setpts=PTS-STARTPTS", "-c:v", "libx264",
        "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-fps_mode", "vfr",
        "-force_key_frames", f"expr:gte(t,n_forced*{seconds})",
        "-f", "segment", "-segment_time", str(seconds), "-reset_timestamps", "1",
        "-segment_list_type", "csv", "-segment_list", "pipe:1", str(directory / "part-%06d.mkv"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")},
    )
    try:
        while line := await asyncio.wait_for(process.stdout.readline(), timeout=seconds + 30):
            try:
                name, start, end = next(csv.reader([line.decode()]))
                start, end = float(start), float(end)
                if not re.fullmatch(r"part-\d{6,}\.mkv", name) or not math.isfinite(start) or not math.isfinite(end) \
                        or not 0 < end - start <= seconds + 2 or start < 0:
                    raise ValueError
            except (ValueError, UnicodeError, csv.Error):
                raise MediaError("连续录像片段索引无效") from None
            yield directory / name, start, end
        await process.wait()
        if process.returncode:
            raise MediaError("连续取流已中断，请检查摄像头和桥接服务")
    except TimeoutError:
        raise MediaError("连续取流超时，未收到新片段") from None
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


async def accelerate(source: Path, target: Path, speed: float, sample_fps: float = 2):
    await command(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(source), "-an",
        "-vf", f"setpts=(PTS-STARTPTS)/{speed},fps={sample_fps * speed},scale=640:-2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(target), timeout=90,
    )
    if not 0 < target.stat().st_size <= 32 * 1024 * 1024:
        raise MediaError("加速视频为空或超过 32 MiB")


async def screenshot(source: Path, target: Path, offset: float):
    target.unlink(missing_ok=True)
    await command(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", str(offset), "-i", str(source),
        "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-pix_fmt", "yuvj420p", "-q:v", "3",
        "-update", "1", str(target), timeout=30,
    )
    if not target.exists() or not target.stat().st_size:
        raise MediaError("无法截取视频画面")
