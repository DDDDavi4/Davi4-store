"""
SenseVoice Teleprompter - 后端服务
SenseVoiceSmall 实时语音识别，支持三种模式：
1. 语音识别模式 - 实时语音转文字
2. 提词器模式   - 朗读跟踪 + 高亮当前行
3. 导播模式     - 段落定位 + 镜头切换提示

识别引擎：FunASR SenseVoiceSmall (CPU) — 中文高准确率，自带 fsmn-vad

音频采集方式：浏览器端 getUserMedia 采集，通过 WebSocket 发送 base64 PCM 到后端
不依赖 sounddevice/PortAudio，任意电脑无需额外驱动即可运行
"""

import os
import sys
import asyncio
import json
import time
import threading
import re
import queue
import base64
import struct
from typing import Optional
from pathlib import Path
from collections import deque

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import difflib

# ============ 配置 ============
SAMPLE_RATE = 16000
CHUNK_SIZE = 4096  # ~256ms per chunk at 16kHz

# 音频累积时间（秒）：可由前端动态调整
# SenseVoice 自带 fsmn-vad 会自动切分语音段，所以不需要外层 VAD
accumulate_seconds = 1.0

def get_accumulate_samples():
    return int(SAMPLE_RATE * accumulate_seconds)

# 兜底定时器：如果累积超长没有识别，强制触发
MAX_BUFFER_SECONDS = 8.0
MAX_BUFFER_SAMPLES = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)

# 匹配窗口
MATCH_WINDOW_SIZE = 12

app = FastAPI(title="SenseVoice Teleprompter")

# ============ 全局模型状态 ============
sensevoice_model = None
sensevoice_loading = False
sensevoice_loaded = False
sensevoice_error: Optional[str] = None


def load_sensevoice_model():
    """加载 SenseVoice 模型（CPU 模式）"""
    global sensevoice_model, sensevoice_loading, sensevoice_loaded, sensevoice_error
    sensevoice_loading = True
    try:
        from funasr.models.sense_voice.model import SenseVoiceSmall  # 注册模型类
        from funasr import AutoModel

        # 便携模式：优先从本地 models/ 目录加载
        base_dir = Path(__file__).parent
        local_sensevoice = base_dir / "models" / "SenseVoiceSmall"
        local_vad = base_dir / "models" / "fsmn-vad"

        model_path = str(local_sensevoice) if local_sensevoice.exists() else "iic/SenseVoiceSmall"
        vad_path = str(local_vad) if local_vad.exists() else "fsmn-vad"

        src = "本地 models/" if local_sensevoice.exists() else "ModelScope 在线"
        print(f"[SenseVoice] 正在加载 SenseVoiceSmall + fsmn-vad (CPU, 来源: {src})...")
        t0 = time.time()
        sensevoice_model = AutoModel(
            model=model_path,
            vad_model=vad_path,
            vad_kwargs={"max_single_segment_time": 6000},
            device="cpu",
            ncpu=8,
            disable_update=True,
        )
        sensevoice_loaded = True
        elapsed = time.time() - t0
        print(f"[SenseVoice] 加载完成! ({elapsed:.1f}s)")
    except Exception as e:
        sensevoice_error = str(e)
        print(f"[SenseVoice] 加载失败: {e}")
    finally:
        sensevoice_loading = False


# 启动时后台加载模型
threading.Thread(target=load_sensevoice_model, daemon=True).start()


# ============ 音频设备（前端浏览器枚举，后端不再需要） ============

@app.get("/api/devices")
def get_audio_devices():
    """返回空列表，设备枚举由前端浏览器原生实现"""
    return {"devices": []}


# ============ 模型状态 ============

@app.get("/api/model-status")
def get_model_status():
    return {
        "engine": "sensevoice",
        "loading": sensevoice_loading,
        "loaded": sensevoice_loaded,
        "error": sensevoice_error,
    }

@app.get("/api/chunk-seconds")
def get_chunk_seconds():
    return {"seconds": accumulate_seconds}


# ============ 文本匹配工具 ============

def normalize_text(text: str) -> str:
    """标准化文本用于比对：去标点、去空白、小写"""
    text = re.sub(r'[^\w]', '', text)
    return text.lower()


def find_matching_line(script_lines: list[str], recent_texts: list[str], current_line: int) -> dict:
    if not recent_texts or not script_lines:
        return {"line": current_line, "confidence": 0.0, "line_progress": 0.0}

    # 只用最近2段匹配（1秒一段，2秒窗口足够判断当前进度）
    # 用太多历史文本会导致当前行一直能匹配上，无法前进
    recent_short = "".join(recent_texts[-2:])
    spoken_norm = normalize_text(recent_short)
    if not spoken_norm:
        return {"line": current_line, "confidence": 0.0, "line_progress": 0.0}

    # 匹配范围：当前行 ±3，前瞻到 +8（读词可能比识别快）
    start = max(0, current_line - 3)
    end = min(len(script_lines), current_line + 9)

    best_match = current_line
    best_ratio = 0.0

    for i in range(start, end):
        line_text = script_lines[i].strip()
        if not line_text:
            continue
        line_norm = normalize_text(line_text)
        if not line_norm:
            continue

        ratio = 0.0
        if line_norm in spoken_norm:
            ratio = 0.7 + 0.3 * (len(line_norm) / max(len(spoken_norm), 1))
        elif spoken_norm in line_norm:
            ratio = 0.5 + 0.3 * (len(spoken_norm) / max(len(line_norm), 1))
        else:
            sm = difflib.SequenceMatcher(None, spoken_norm[-len(line_norm)*2:], line_norm)
            ratio = sm.ratio() * 0.6

        distance = abs(i - current_line)
        if i < current_line:
            if distance > 1:
                ratio *= 0.3
            else:
                ratio *= 0.7
        elif i > current_line:
            ratio *= max(0.7, 1.0 - distance * 0.03)

        if ratio > best_ratio:
            best_ratio = ratio
            best_match = i

    if best_ratio < 0.25:
        return {"line": current_line, "confidence": best_ratio, "line_progress": 0.0}

    # 如果当前行匹配度很高（进度>0.7），检查下一行是否也开始匹配了
    # 读词比识别快时，最新语音可能已经到了下一句
    if best_match == current_line:
        matched_line_norm = normalize_text(script_lines[current_line].strip())
        recent_spoken = normalize_text(recent_short)
        if matched_line_norm and recent_spoken:
            if matched_line_norm in recent_spoken:
                # 当前行已完整出现在最近语音中，检查下一行
                if current_line + 1 < len(script_lines):
                    next_norm = normalize_text(script_lines[current_line + 1].strip())
                    if next_norm:
                        # 最新语音中去除当前行文本后，剩余部分是否匹配下一行
                        remaining = recent_spoken.replace(matched_line_norm, "").strip()
                        if remaining and (next_norm in remaining or remaining in next_norm):
                            best_match = current_line + 1
                            best_ratio = 0.6

    return {
        "line": best_match,
        "confidence": round(best_ratio, 2),
        "line_progress": 0.0,
    }




def find_current_segment(segments: list[dict], recent_texts: list[str], current_segment_idx: int) -> int:
    if not recent_texts or not segments:
        return current_segment_idx

    # 只用最近2段匹配（和提词器逻辑一致，避免历史文本太长导致当前段始终匹配）
    recent_short = "".join(recent_texts[-2:])
    spoken_norm = normalize_text(recent_short)
    if not spoken_norm:
        return current_segment_idx

    best_match = current_segment_idx
    best_ratio = 0.0

    # 匹配范围：当前段 ±1，前瞻到 +8（读词可能比识别快）
    start = max(0, current_segment_idx - 1)
    end = min(len(segments), current_segment_idx + 9)

    for i in range(start, end):
        seg_text = segments[i].get("text", "").strip()
        seg_norm = normalize_text(seg_text)
        if not seg_norm:
            continue

        ratio = 0.0
        if seg_norm in spoken_norm:
            ratio = 0.7 + 0.3 * (len(seg_norm) / max(len(spoken_norm), 1))
        elif spoken_norm in seg_norm:
            ratio = 0.5 + 0.3 * (len(spoken_norm) / max(len(seg_norm), 1))
        else:
            sm = difflib.SequenceMatcher(None, spoken_norm[-len(seg_norm)*2:], seg_norm)
            ratio = sm.ratio() * 0.6

        distance = abs(i - current_segment_idx)
        if i < current_segment_idx:
            ratio *= 0.7
        elif i > current_segment_idx:
            ratio *= max(0.7, 1.0 - distance * 0.03)

        if ratio > best_ratio:
            best_ratio = ratio
            best_match = i

    if best_ratio < 0.25:
        return current_segment_idx

    # 当当前段已完整匹配时，检查是否已经读到下一段
    if best_match == current_segment_idx:
        matched_norm = normalize_text(segments[current_segment_idx].get("text", "").strip())
        recent_spoken = normalize_text(recent_short)
        if matched_norm and recent_spoken:
            if matched_norm in recent_spoken:
                if current_segment_idx + 1 < len(segments):
                    next_norm = normalize_text(segments[current_segment_idx + 1].get("text", "").strip())
                    if next_norm:
                        remaining = recent_spoken.replace(matched_norm, "").strip()
                        if remaining and (next_norm in remaining or remaining in next_norm):
                            best_match = current_segment_idx + 1
                            best_ratio = 0.6

    return best_match


# ============ 浏览器端音频接收（替代 sounddevice AudioRecorder） ============
# 前端通过 getUserMedia 采集音频，WebSocket 发送 base64 编码的 Int16 PCM
# 后端解码后累积并喂给 SenseVoice

def decode_pcm_from_base64(b64_data: str) -> np.ndarray:
    """将 base64 编码的 Int16 PCM 解码为 Float32 numpy 数组"""
    try:
        raw = base64.b64decode(b64_data)
        # Int16 little-endian -> float32 [-1, 1]
        int16_arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return int16_arr
    except Exception as e:
        print(f"[Audio] 解码失败: {e}")
        return np.array([], dtype=np.float32)


# ============ SenseVoice 识别 ============

def do_sensevoice_recognize(audio_data: np.ndarray) -> str:
    """
    执行 SenseVoice 识别。
    SenseVoiceSmall 自带 fsmn-vad，能自动处理长音频分段。
    输出需要 rich_transcription_postprocess 去除特殊标签。
    """
    global sensevoice_model
    if sensevoice_model is None:
        return ""
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        t0 = time.time()
        res = sensevoice_model.generate(
            input=audio_data,
            language="zh",
            use_itn=True,
        )
        # 提取文本并去除特殊标签（<|NEUTRAL|> <|Speech|> 等）
        raw_text = res[0]["text"] if res else ""
        text = rich_transcription_postprocess(raw_text)

        elapsed = time.time() - t0
        audio_duration = len(audio_data) / SAMPLE_RATE
        rtf = elapsed / audio_duration if audio_duration > 0 else 0
        print(f"[SenseVoice] {audio_duration:.1f}s -> {elapsed:.2f}s (RTF={rtf:.2f}) \"{text[:50]}\"")

        return text.strip()
    except Exception as e:
        print(f"[SenseVoice] 识别错误: {e}")
        return ""


# ============ WebSocket 实时语音识别 ============

async def _send_transcription(ws, mode, text, full_text,
                               script_lines, segments, current_line,
                               current_segment_idx, recent_texts):
    response = {
        "type": "transcription",
        "text": text,
        "full_text": full_text,
    }

    if mode == "teleprompter" and script_lines:
        recent_list = list(recent_texts)
        match_result = find_matching_line(script_lines, recent_list, current_line)
        current_line = match_result["line"]
        response["current_line"] = current_line
        response["total_lines"] = len(script_lines)
        response["match_confidence"] = match_result["confidence"]
        response["line_progress"] = match_result["line_progress"]

    if mode == "director" and segments:
        recent_list = list(recent_texts)
        new_seg = find_current_segment(segments, recent_list, current_segment_idx)
        current_segment_idx = new_seg
        response["current_segment"] = current_segment_idx
        response["total_segments"] = len(segments)
        if current_segment_idx < len(segments):
            response["current_camera"] = segments[current_segment_idx].get("camera", "")
            response["current_notes"] = segments[current_segment_idx].get("notes", "")
            response["current_segment_text"] = segments[current_segment_idx].get("text", "")
        if current_segment_idx + 1 < len(segments):
            response["next_camera"] = segments[current_segment_idx + 1].get("camera", "")
            response["next_notes"] = segments[current_segment_idx + 1].get("notes", "")
            response["next_segment_text"] = segments[current_segment_idx + 1].get("text", "")
        else:
            response["next_camera"] = "(最后一段)"
            response["next_notes"] = ""

    try:
        await ws.send_json(response)
    except Exception:
        pass

    return current_line, current_segment_idx


@app.websocket("/ws/transcribe")
async def websocket_transcribe(ws: WebSocket):
    await ws.accept()

    global accumulate_seconds
    mode = "recognition"
    script_lines = []
    current_line = 0
    segments = []
    current_segment_idx = 0

    full_text = ""
    recent_texts = deque(maxlen=MATCH_WINDOW_SIZE)
    ws_recording = False  # 前端是否在录音

    # ===== 识别线程 + 结果队列 =====
    recognize_result_queue = queue.Queue()
    is_recognizing = threading.Event()

    def _run_recognize_in_thread(audio_data: np.ndarray):
        """在线程中执行识别，结果放入队列"""
        try:
            text = do_sensevoice_recognize(audio_data)
            recognize_result_queue.put((text, len(audio_data) / SAMPLE_RATE))
        except Exception as e:
            recognize_result_queue.put(("", 0))
        finally:
            is_recognizing.clear()

    def _start_recognize(audio_data: np.ndarray):
        """启动识别线程（非阻塞）"""
        if is_recognizing.is_set():
            return
        is_recognizing.set()
        t = threading.Thread(target=_run_recognize_in_thread, args=(audio_data,), daemon=True)
        t.start()

    # ===== 音频缓冲区（定时累积喂给 SenseVoice） =====
    audio_buffer = np.array([], dtype=np.float32)
    last_recognize_time = 0.0

    try:
        while True:
            # ---- 第一步：接收前端消息（非阻塞） ----
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=0.01)
                msg = json.loads(data)
                cmd = msg.get("cmd")

                if cmd == "set_mode":
                    mode = msg["mode"]
                    await ws.send_json({"type": "mode_changed", "mode": mode})

                elif cmd == "start":
                    if not sensevoice_loaded:
                        await ws.send_json({"type": "error", "message": "SenseVoice 模型尚未加载完成，请稍候..."})
                        continue
                    ws_recording = True
                    audio_buffer = np.array([], dtype=np.float32)
                    last_recognize_time = time.time()
                    await ws.send_json({"type": "started"})

                elif cmd == "stop":
                    ws_recording = False
                    audio_buffer = np.array([], dtype=np.float32)
                    await ws.send_json({"type": "stopped"})

                elif cmd == "audio_data":
                    # 接收前端浏览器发来的 PCM 音频数据
                    if ws_recording and not is_recognizing.is_set():
                        b64 = msg.get("data", "")
                        pcm_float = decode_pcm_from_base64(b64)
                        if len(pcm_float) > 0:
                            audio_buffer = np.concatenate([audio_buffer, pcm_float])

                            # 达到累积时间且有足够能量，触发识别
                            now = time.time()
                            time_since_last = now - last_recognize_time
                            buffer_duration = len(audio_buffer) / SAMPLE_RATE

                            if len(audio_buffer) >= get_accumulate_samples() and time_since_last > max(0.5, accumulate_seconds - 0.3):
                                rms = float(np.sqrt(np.mean(audio_buffer ** 2)))
                                if rms > 0.005:
                                    process_audio = audio_buffer.copy()
                                    audio_buffer = np.array([], dtype=np.float32)
                                    last_recognize_time = now
                                    _start_recognize(process_audio)

                            # 兜底：缓冲区超长强制处理
                            if len(audio_buffer) >= MAX_BUFFER_SAMPLES:
                                rms = float(np.sqrt(np.mean(audio_buffer ** 2)))
                                if rms > 0.005:
                                    process_audio = audio_buffer.copy()
                                    audio_buffer = np.array([], dtype=np.float32)
                                    last_recognize_time = now
                                    _start_recognize(process_audio)
                                else:
                                    audio_buffer = np.array([], dtype=np.float32)

                            # 计算音量并发送
                            rms = float(np.sqrt(np.mean(pcm_float ** 2)))
                            volume = min(100, int(rms * 500))
                            try:
                                await ws.send_json({"type": "volume", "value": volume})
                            except Exception:
                                pass

                elif cmd == "reset":
                    full_text = ""
                    recent_texts.clear()
                    current_line = 0
                    current_segment_idx = 0
                    audio_buffer = np.array([], dtype=np.float32)
                    last_recognize_time = time.time()
                    await ws.send_json({"type": "reset_done"})

                elif cmd == "set_chunk_seconds":
                    val = msg.get("seconds", 1.0)
                    val = max(0.5, min(5.0, float(val)))  # 限制 0.5~5秒
                    accumulate_seconds = val
                    await ws.send_json({"type": "chunk_seconds_changed", "seconds": accumulate_seconds})

                elif cmd == "load_script":
                    script_text = msg.get("text", "")
                    script_lines = [line for line in script_text.split("\n") if line.strip()]
                    current_line = 0

                elif cmd == "jump_line":
                    jump_to = msg.get("line")
                    if jump_to is not None and isinstance(jump_to, int):
                        current_line = jump_to
                        # 清除最近识别文本，让匹配从新的选中点重新开始
                        recent_texts.clear()
                        print(f"[Teleprompter] 鼠标点击跳转到第 {jump_to + 1} 行，匹配起点已重置")
                    await ws.send_json({
                        "type": "line_jumped",
                        "current_line": current_line,
                        "total_lines": len(script_lines),
                    })

                elif cmd == "jump_segment":
                    jump_to = msg.get("segment")
                    if jump_to is not None and isinstance(jump_to, int):
                        current_segment_idx = jump_to
                        recent_texts.clear()
                        print(f"[Director] 鼠标点击跳转到第 {jump_to + 1} 段，匹配起点已重置")
                    await ws.send_json({
                        "type": "segments_loaded",
                        "segment_count": len(segments),
                        "segments": segments,
                    })

                elif cmd == "load_segments":
                    segments = msg.get("segments", [])
                    current_segment_idx = 0
                    await ws.send_json({
                        "type": "segments_loaded",
                        "segment_count": len(segments),
                        "segments": segments,
                    })

            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                pass

            # ---- 第二步：检查识别结果 ----
            while not recognize_result_queue.empty():
                try:
                    recognized, audio_dur = recognize_result_queue.get_nowait()
                except queue.Empty:
                    break

                if recognized:
                    full_text += recognized
                    recent_texts.append(recognized)
                    current_line, current_segment_idx = await _send_transcription(
                        ws, mode, recognized, full_text,
                        script_lines, segments, current_line,
                        current_segment_idx, recent_texts)

            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        pass
    finally:
        ws_recording = False
        print("[WS] 客户端断开连接")


# ============ 静态文件与前端 ============

frontend_dir = Path(__file__).parent / "frontend"
frontend_dir.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
async def serve_frontend():
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>前端文件未找到，请确保 frontend/index.html 存在</h1>")
