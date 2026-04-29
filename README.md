# SenseVoice Teleprompter

基于阿里 FunASR SenseVoiceSmall 的实时语音提词器，纯 CPU 即可运行，中文识别准确度极高。

## 功能

### 🎤 语音识别模式
- 实时语音转文字，浏览器麦克风采集，WebSocket 传输
- 自动统计识别字数、段数、时长
- 支持清除转写内容

### 📝 提词器模式
- 加载台词文稿，语音驱动行号自动跟踪
- 当前行高亮 + 下一句预提示
- **鼠标点击任意行跳转**，语音匹配从点击位置重新开始
- 字号可调节

### 🎬 导播模式
- 加载导播文稿（Excel/JSON），自动识别"文稿"和"镜头"列
- 语音驱动段落切换，实时显示当前镜头、备注、下一镜头
- **鼠标点击任意段跳转**，语音匹配从点击位置重新开始
- 段落进度条 + 手动微调

## 快速启动

### 方式一：一键启动（推荐）
双击 `一键启动.bat`，首次运行自动下载 Python + 依赖 + 模型，之后直接启动服务并打开浏览器。

### 方式二：手动启动
```bash
pip install fastapi uvicorn websockets funasr modelscope torch torchaudio six torch_complex --extra-index-url https://download.pytorch.org/whl/cpu
python -m uvicorn server:app --host 127.0.0.1 --port 8765
```
浏览器打开 http://localhost:8765

## 技术架构

- **语音引擎**：FunASR SenseVoiceSmall（自带 fsmn-vad，无需额外 VAD）
- **后端**：FastAPI + WebSocket（实时音频流 + 识别结果推送）
- **前端**：单页 HTML，浏览器 getUserMedia 采集音频，零依赖
- **模型加载**：优先本地 `models/` 目录，回退 ModelScope 在线下载

## 文件说明

| 文件 | 说明 |
|------|------|
| `server.py` | 后端服务（FastAPI + SenseVoice） |
| `frontend/index.html` | 前端页面（三模式合一） |
| `launcher.py` | 一键启动器（自动等就绪 + 开浏览器） |
| `一键启动.bat` | Windows 双击启动脚本 |
| `示例台词.txt` | 提词器模式示例文稿 |
| `示例段落.json` | 导播模式示例文稿 |
| `导播文稿模板.xlsx` | 导播模式 Excel 模板 |

## 便携版

将整个目录拷贝到任意 Windows 电脑，双击 `一键启动.bat` 即可运行，无需安装 Python 或任何依赖。
