# astrbot-plugin-discord-voice-tts

Discord 语音频道 TTS 插件，支持将文本转换为语音并在 Discord 语音频道播放。

## 功能特性

- ✅ 监听文字频道消息，触发 TTS
- ✅ 支持 edge-tts 和 AstrBot TTS 模块
- ✅ 自动连接语音频道并播放音频
- ✅ 支持多语音请求排队处理
- ✅ 支持打断当前播放
- ✅ 可配置语音、语速、音量
- ✅ 丰富的命令系统

## 系统要求

- Python 3.8+
- FFmpeg (需要添加到系统 PATH)
- pynacl (用于 Discord 语音)
- edge-tts (推荐) 或 AstrBot TTS 模块

## 安装依赖

```bash
# 安装 Python 依赖
pip install edge-tts pynacl discord.py

# 安装 FFmpeg (Windows)
# 下载 FFmpeg: https://ffmpeg.org/download.html
# 解压并将 bin 目录添加到系统 PATH

# 安装 FFmpeg (Linux)
sudo apt update
sudo apt install ffmpeg

# 安装 FFmpeg (macOS)
brew install ffmpeg
```

## 快速开始

1. 将插件文件夹复制到 AstrBot 的插件目录
2. 安装依赖：`pip install -r requirements.txt`
3. 重启 AstrBot
4. 在 Discord 中使用 `/tts_help` 查看帮助

## 命令列表

| 命令 | 说明 | 用法 |
|------|------|------|
| `/tts_join` | 加入语音频道 | `/tts_join` |
| `/tts_leave` | 离开语音频道 | `/tts_leave` |
| `/tts` | 将文本转换为语音并播放 | `/tts 你好，世界！` |
| `/tts_stop` | 停止当前 TTS 播放 | `/tts_stop` |
| `/tts_config` | 配置 TTS 设置 | `/tts_config voice zh-CN-YunxiNeural` |
| `/tts_status` | 查看 TTS 状态 | `/tts_status` |
| `/tts_help` | 显示帮助信息 | `/tts_help` |

## 使用示例

### 基本使用

```
# 1. 加入语音频道
/tts_join

# 2. 播放 TTS
/tts 你好，这是一个测试消息

# 3. 停止播放
/tts_stop

# 4. 离开语音频道
/tts_leave
```

### 配置 TTS

```
# 设置语音
/tts_config voice zh-CN-YunxiNeural

# 设置语速 (加快 20%)
/tts_config rate +20%

# 设置音量 (增加 10%)
/tts_config volume +10%
```

### 查看状态

```
# 查看当前 TTS 状态
/tts_status
```

## 支持的语音

### 中文语音 (edge-tts)

| 语音名称 | 描述 |
|---------|------|
| zh-CN-XiaoxiaoNeural | 女声，温柔 |
| zh-CN-YunxiNeural | 男声，活泼 |
| zh-CN-YunjianNeural | 男声，沉稳 |
| zh-CN-XiaoyiNeural | 女声，甜美 |
| zh-CN-YunyangNeural | 男声，专业 |
| zh-CN-XiaochenNeural | 女声，自然 |

### 英文语音 (edge-tts)

| 语音名称 | 描述 |
|---------|------|
| en-US-AriaNeural | 女声，自然 |
| en-US-GuyNeural | 男声，自然 |
| en-US-JennyNeural | 女声，友好 |
| en-US-DavisNeural | 男声，自信 |

更多语音请参考: [edge-tts 语音列表](https://github.com/rany2/edge-tts/blob/master/README.md#available-voices)

## 架构说明

### 核心组件

1. **TTS 音频生成**
   - 优先使用 edge-tts (高质量，免费)
   - 备选使用 AstrBot TTS 模块
   - 支持自定义语音、语速、音量

2. **语音频道管理**
   - 自动连接/断开语音频道
   - 支持多服务器同时使用
   - 连接状态监控

3. **音频播放队列**
   - 异步队列处理多个 TTS 请求
   - 支持打断当前播放
   - 自动清理临时文件

4. **命令系统**
   - 丰富的配置命令
   - 状态查询
   - 帮助文档

### 技术要点

- **异步处理**: 使用 asyncio 避免阻塞主线程
- **队列机制**: 使用 asyncio.Queue 管理播放队列
- **资源管理**: 自动清理临时文件和语音连接
- **错误处理**: 完善的异常处理和日志记录

## 注意事项

1. **FFmpeg 依赖**: 必须安装 FFmpeg 并添加到系统 PATH
2. **pynacl 依赖**: Discord 语音需要 pynacl 库
3. **网络要求**: edge-tts 需要网络连接
4. **权限要求**: Bot 需要语音频道连接权限
5. **性能考虑**: 长文本可能需要较长生成时间

## 故障排除

### 无法连接语音频道

- 检查 Bot 是否有语音频道连接权限
- 确认 pynacl 已正确安装
- 查看日志中的错误信息

### TTS 生成失败

- 确认 edge-tts 已安装: `pip install edge-tts`
- 检查网络连接
- 尝试使用不同的语音

### 播放无声

- 确认 FFmpeg 已安装并添加到 PATH
- 检查音频文件是否生成成功
- 查看日志中的错误信息

### 队列堵塞

- 使用 `/tts_stop` 清空队列
- 检查是否有长时间运行的 TTS 任务
- 重启插件

## 开发说明

### 代码结构

```
main.py
├── DiscordVoiceTTS 类
│   ├── __init__ - 初始化
│   ├── initialize - 插件初始化
│   ├── terminate - 插件销毁
│   ├── generate_tts_audio - 生成 TTS 音频
│   ├── play_audio_queue - 播放音频队列
│   ├── connect_to_voice_channel - 连接语音频道
│   ├── disconnect_from_voice_channel - 断开语音频道
│   ├── interrupt_current_playback - 打断当前播放
│   └── 命令处理函数
│       ├── join_voice_channel
│       ├── leave_voice_channel
│       ├── tts_command
│       ├── stop_tts
│       ├── config_tts
│       ├── tts_status
│       └── tts_help
```

### 扩展功能

1. **自动 TTS**: 监听特定频道消息，自动转换为语音
2. **语音识别**: 集成语音识别，实现语音转文字
3. **多语言支持**: 支持更多语言和语音
4. **音效处理**: 添加音效和背景音乐
5. **Web 管理界面**: 提供 Web 界面管理 TTS 设置

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

## 相关链接

- [AstrBot 官方文档](https://docs.astrbot.app)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [edge-tts GitHub](https://github.com/rany2/edge-tts)
- [discord.py 文档](https://discordpy.readthedocs.io/)
- [FFmpeg 官网](https://ffmpeg.org/)
