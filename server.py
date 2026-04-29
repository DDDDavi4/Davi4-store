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

# ============ 切片模式 ============
# "timer"  = 固定时间切片（accumulate_seconds），简单直接
# "breath" = 气口切片，检测静音间隙在气口处切片，识别更准确
slice_mode = "timer"

# 气口切片参数
BREATH_SILENCE_THRESHOLD = 0.008   # 静音能量阈值（RMS）
BREATH_SILENCE_MIN_MS = 250        # 静音最短持续时间（毫秒），短于此不算气口
BREATH_SPEECH_MIN_MS = 400         # 语音最短持续时间（毫秒），短于此不切片
BREATH_MAX_SECONDS = 6.0           # 气口模式最大累积时长，超过强制切片
BREATH_MAX_SAMPLES = int(SAMPLE_RATE * BREATH_MAX_SECONDS)

# 匹配窗口
MATCH_WINDOW_SIZE = 12

app = FastAPI(title="SenseVoice Teleprompter")

# ============ 模型注册表 ============
# 支持多个 FunASR 模型，可通过前端切换
MODEL_REGISTRY = {
    "SenseVoiceSmall": {
        "display_name": "SenseVoice 小型",
        "description": "234M 参数，自带 VAD + 情感识别，推理极快",
        "model_id": "iic/SenseVoiceSmall",          # ModelScope ID
        "local_dir": "SenseVoiceSmall",              # 本地 models/ 子目录名
        "vad_model_id": "fsmn-vad",
        "vad_local_dir": "fsmn-vad",
        "need_vad": True,
        "need_sensevoice_import": True,              # 需要额外 import 注册模型类
        "language": "zh",
        "use_itn": True,
    },
    "paraformer-zh": {
        "display_name": "Paraformer 中文",
        "description": "220M 参数，中文识别精度更高 (CER 1.95%)，支持流式",
        "model_id": "paraformer-zh",                  # ModelScope ID
        "local_dir": "paraformer-zh",                 # 本地 models/ 子目录名
        "vad_model_id": "fsmn-vad",
        "vad_local_dir": "fsmn-vad",
        "need_vad": True,
        "need_sensevoice_import": False,
        "language": "zh",
        "use_itn": True,
    },
}

# 当前选中的模型 ID
current_model_id = "SenseVoiceSmall"

# ============ 全局模型状态 ============
sensevoice_model = None
sensevoice_loading = False
sensevoice_loaded = False
sensevoice_error: Optional[str] = None


def _resolve_model_path(model_cfg: dict) -> tuple:
    """根据本地/在线策略解析模型和 VAD 的路径"""
    base_dir = Path(__file__).parent

    # 模型路径
    local_model = base_dir / "models" / model_cfg["local_dir"]
    model_path = str(local_model) if local_model.exists() else model_cfg["model_id"]
    model_src = "本地 models/" if local_model.exists() else "ModelScope 在线"

    # VAD 路径
    if model_cfg.get("need_vad"):
        local_vad = base_dir / "models" / model_cfg["vad_local_dir"]
        vad_path = str(local_vad) if local_vad.exists() else model_cfg["vad_model_id"]
        vad_src = "本地 models/" if local_vad.exists() else "ModelScope 在线"
    else:
        vad_path = None
        vad_src = "-"

    return model_path, model_src, vad_path, vad_src


def load_sensevoice_model(model_id: str = None):
    """加载指定模型（CPU 模式）"""
    global sensevoice_model, sensevoice_loading, sensevoice_loaded, sensevoice_error, current_model_id

    if model_id is None:
        model_id = current_model_id

    if model_id not in MODEL_REGISTRY:
        print(f"[Model] 未知模型: {model_id}，可用: {list(MODEL_REGISTRY.keys())}")
        return

    sensevoice_loading = True
    sensevoice_loaded = False
    sensevoice_error = None

    try:
        from funasr import AutoModel

        model_cfg = MODEL_REGISTRY[model_id]
        model_path, model_src, vad_path, vad_src = _resolve_model_path(model_cfg)

        # 某些模型需要额外 import 触发注册
        if model_cfg.get("need_sensevoice_import"):
            from funasr.models.sense_voice.model import SenseVoiceSmall  # 注册模型类

        print(f"[Model] 正在加载 {model_cfg['display_name']} ({model_id})")
        print(f"[Model]   模型来源: {model_src}  |  VAD 来源: {vad_src}")

        t0 = time.time()

        auto_kwargs = dict(
            model=model_path,
            device="cpu",
            ncpu=8,
            disable_update=True,
        )

        # 如果模型需要 VAD
        if vad_path:
            auto_kwargs["vad_model"] = vad_path
            auto_kwargs["vad_kwargs"] = {"max_single_segment_time": 6000}

        sensevoice_model = AutoModel(**auto_kwargs)

        current_model_id = model_id
        sensevoice_loaded = True
        elapsed = time.time() - t0
        print(f"[Model] {model_cfg['display_name']} 加载完成! ({elapsed:.1f}s)")
    except Exception as e:
        sensevoice_error = str(e)
        print(f"[Model] 加载失败: {e}")
    finally:
        sensevoice_loading = False


# 启动时后台加载默认模型
threading.Thread(target=load_sensevoice_model, daemon=True).start()


# ============ 音频设备（前端浏览器枚举，后端不再需要） ============

@app.get("/api/devices")
def get_audio_devices():
    """返回空列表，设备枚举由前端浏览器原生实现"""
    return {"devices": []}


# ============ 模型状态 ============

@app.get("/api/model-status")
def get_model_status():
    model_cfg = MODEL_REGISTRY.get(current_model_id, {})
    return {
        "engine": "sensevoice",
        "model_id": current_model_id,
        "model_name": model_cfg.get("display_name", current_model_id),
        "model_desc": model_cfg.get("description", ""),
        "loading": sensevoice_loading,
        "loaded": sensevoice_loaded,
        "error": sensevoice_error,
        "available_models": {
            k: {"name": v["display_name"], "desc": v["description"]}
            for k, v in MODEL_REGISTRY.items()
        },
    }


@app.post("/api/switch-model")
async def switch_model_api(request):
    """通过 HTTP API 切换模型"""
    import json as _json
    body = await request.json()
    model_id = body.get("model_id", "")
    return await _do_switch_model(model_id)


async def _do_switch_model(model_id: str):
    """执行模型切换逻辑"""
    global sensevoice_model, sensevoice_loaded, sensevoice_error

    if model_id not in MODEL_REGISTRY:
        return {"ok": False, "error": f"未知模型: {model_id}"}
    if model_id == current_model_id and sensevoice_loaded:
        return {"ok": False, "error": f"已在使用 {MODEL_REGISTRY[model_id]['display_name']}"}

    # 卸载旧模型
    print(f"[Model] 卸载当前模型，准备切换到 {model_id}...")
    sensevoice_model = None
    sensevoice_loaded = False
    sensevoice_error = None

    # 后台加载新模型
    def _load():
        load_sensevoice_model(model_id)
    t = threading.Thread(target=_load, daemon=True)
    t.start()

    return {"ok": True, "message": f"正在切换到 {MODEL_REGISTRY[model_id]['display_name']}..."}

@app.get("/api/chunk-seconds")
def get_chunk_seconds():
    return {"seconds": accumulate_seconds}


@app.get("/api/slice-mode")
def get_slice_mode():
    return {"mode": slice_mode, "description": "timer = fixed interval, breath = silence gap detection"}


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


def find_breath_split_point(audio_data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> int:
    """
    在音频数据中寻找最佳气口（静音间隙）位置用于切片。
    
    策略：
    1. 将音频分帧（每帧 10ms），计算每帧 RMS 能量
    2. 找到所有静音帧（RMS < 阈值）组成的连续区间
    3. 在音频后半段（40%~100%）找最长静音区间的中点作为切片点
    4. 如果没有找到合适的气口，返回 -1（由上层决定是否强制切片）
    
    返回：切片点样本索引，-1 表示未找到
    """
    frame_size = int(sample_rate * 0.01)  # 10ms per frame
    n_frames = len(audio_data) // frame_size
    if n_frames < 10:
        return -1
    
    # 计算每帧 RMS
    energies = np.zeros(n_frames)
    for i in range(n_frames):
        frame = audio_data[i * frame_size : (i + 1) * frame_size]
        energies[i] = float(np.sqrt(np.mean(frame ** 2)))
    
    # 找静音帧（RMS < 阈值）
    is_silent = energies < BREATH_SILENCE_THRESHOLD
    
    # 合并连续静音帧为区间
    min_silent_frames = int(BREATH_SILENCE_MIN_MS / 10)  # 250ms = 25帧
    min_speech_frames = int(BREATH_SPEECH_MIN_MS / 10)    # 400ms = 40帧
    
    # 只在音频后半段找气口（保证前面有足够语音）
    search_start = max(min_speech_frames, n_frames // 3)
    
    best_split = -1
    best_len = 0
    
    # 遍历寻找连续静音区间
    i = search_start
    while i < n_frames:
        if is_silent[i]:
            # 找到连续静音的起止
            start = i
            while i < n_frames and is_silent[i]:
                i += 1
            silence_len = i - start
            
            # 静音区间够长才算气口
            if silence_len >= min_silent_frames and silence_len > best_len:
                best_len = silence_len
                # 取静音区间中点偏前（偏向前一段语音末尾）
                mid = start + silence_len // 3
                best_split = mid * frame_size
        else:
            i += 1
    
    return best_split


# ============ SenseVoice 识别 ============

def do_sensevoice_recognize(audio_data: np.ndarray) -> str:
    """
    执行语音识别。
    自动适配当前加载的模型（SenseVoiceSmall / Paraformer-zh）。
    输出需要 rich_transcription_postprocess 去除特殊标签（SenseVoice 系列需要）。
    """
    global sensevoice_model
    if sensevoice_model is None:
        return ""
    try:
        model_cfg = MODEL_REGISTRY.get(current_model_id, {})
        t0 = time.time()
        res = sensevoice_model.generate(
            input=audio_data,
            language=model_cfg.get("language", "zh"),
            use_itn=model_cfg.get("use_itn", True),
        )
        # 提取文本并去除特殊标签（<|NEUTRAL|> <|Speech|> 等，SenseVoice 系列）
        raw_text = res[0]["text"] if res else ""

        # SenseVoice 模型输出包含特殊标签，需要后处理；Paraformer 不需要
        if model_cfg.get("need_sensevoice_import"):
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
            text = rich_transcription_postprocess(raw_text)
        else:
            text = raw_text

        elapsed = time.time() - t0
        audio_duration = len(audio_data) / SAMPLE_RATE
        rtf = elapsed / audio_duration if audio_duration > 0 else 0
        model_name = model_cfg.get("display_name", current_model_id)
        print(f"[Model:{model_name}] {audio_duration:.1f}s -> {elapsed:.2f}s (RTF={rtf:.2f}) \"{text[:50]}\"")

        return text.strip()
    except Exception as e:
        print(f"[Model] 识别错误: {e}")
        return ""


# ============ WebSocket 实时语音识别 ============

async def _send_transcription(ws, mode, text, full_text,
                               script_lines, segments, current_line,
                               current_segment_idx, recent_texts):
    response = {
        "type": "transcription",
        "text": text,
        "full_text": full_text,
        "model_id": current_model_id,
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

    global accumulate_seconds, slice_mode
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

                            if slice_mode == "breath":
                                # === 气口切片模式：在静音间隙处切片 ===
                                if len(audio_buffer) >= get_accumulate_samples():
                                    # 先检查是否有气口（静音间隙）
                                    split_point = find_breath_split_point(audio_buffer)
                                    if split_point > 0:
                                        # 在气口处切片：前半段送去识别，后半段留在 buffer
                                        speech_part = audio_buffer[:split_point]
                                        rms = float(np.sqrt(np.mean(speech_part ** 2)))
                                        if rms > 0.005:
                                            audio_buffer = audio_buffer[split_point:]
                                            last_recognize_time = time.time()
                                            _start_recognize(speech_part)
                                        else:
                                            # 整段都是静音，清掉
                                            audio_buffer = np.array([], dtype=np.float32)
                                    elif len(audio_buffer) >= BREATH_MAX_SAMPLES:
                                        # 超时兜底：强制切片
                                        rms = float(np.sqrt(np.mean(audio_buffer ** 2)))
                                        if rms > 0.005:
                                            process_audio = audio_buffer.copy()
                                            audio_buffer = np.array([], dtype=np.float32)
                                            last_recognize_time = time.time()
                                            _start_recognize(process_audio)
                                        else:
                                            audio_buffer = np.array([], dtype=np.float32)
                                # 气口模式下也保留绝对兜底
                                if len(audio_buffer) >= MAX_BUFFER_SAMPLES:
                                    rms = float(np.sqrt(np.mean(audio_buffer ** 2)))
                                    if rms > 0.005:
                                        process_audio = audio_buffer.copy()
                                        audio_buffer = np.array([], dtype=np.float32)
                                        last_recognize_time = time.time()
                                        _start_recognize(process_audio)
                                    else:
                                        audio_buffer = np.array([], dtype=np.float32)
                            else:
                                # === 定时切片模式：固定时间累积 ===
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

                elif cmd == "set_slice_mode":
                    new_mode = msg.get("mode", "timer")
                    if new_mode in ("timer", "breath"):
                        slice_mode = new_mode
                        audio_buffer = np.array([], dtype=np.float32)  # 切换模式时清空缓冲区
                        print(f"[SliceMode] 切换到 {'定时切片' if slice_mode == 'timer' else '气口切片'} 模式")
                        await ws.send_json({"type": "slice_mode_changed", "mode": slice_mode})
                    else:
                        await ws.send_json({"type": "error", "message": f"Unknown slice mode: {new_mode}"})

                elif cmd == "set_model":
                    new_model_id = msg.get("model_id", "")
                    if new_model_id and ws_recording:
                        await ws.send_json({"type": "error", "message": "请先停止录音再切换模型"})
                    elif new_model_id:
                        result = await _do_switch_model(new_model_id)
                        if result.get("ok"):
                            await ws.send_json({"type": "model_switching", "model_id": new_model_id, "message": result["message"]})
                        else:
                            await ws.send_json({"type": "error", "message": result.get("error", "切换失败")})
                    else:
                        await ws.send_json({"type": "error", "message": "Missing model_id"})

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
