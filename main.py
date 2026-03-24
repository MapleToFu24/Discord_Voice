import asyncio
import os
import re
import tempfile
from collections import defaultdict
from typing import Dict, Optional
from pathlib import Path

import discord
from discord import VoiceClient, VoiceChannel
from discord.ext import commands

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False
    logger.warning("edge-tts not installed. Chinese TTS functionality will be limited.")

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    logger.warning("aiohttp not installed. VoiceVox TTS functionality will be limited.")

try:
    from astrbot.api import tts as astrbot_tts
    ASTRBOT_TTS_AVAILABLE = True
except ImportError:
    ASTRBOT_TTS_AVAILABLE = False
    logger.warning("AstrBot TTS module not available.")

# ---- Language detection helpers ----

_CJK_JAPANESE_RE = re.compile(
    r'[\u3040-\u309F\u30A0-\u30FF\u31F0-\u31FF]'  # Hiragana / Katakana
)
_CJK_CHINESE_RE = re.compile(
    r'[\u4E00-\u9FFF\u3400-\u4DBF\u20000-\u2A6DF]'  # CJK Unified Ideographs
)


def detect_language(text: str) -> str:
    """Detect whether text is primarily Japanese or Chinese.
    
    Returns:
        'ja' if Japanese characters are found, 'zh' otherwise.
    """
    if _CJK_JAPANESE_RE.search(text):
        return 'ja'
    return 'zh'


@register("discord_voice_tts", "YourName", "Discord 语音频道 TTS 插件", "1.0.0")
class DiscordVoiceTTS(Star):
    """Discord 语音频道 TTS 插件
    
    功能：
    - 监听文字频道消息，触发 TTS
    - 支持 edge-tts 和 AstrBot TTS 模块
    - 自动连接语音频道并播放音频
    - 支持多语音请求排队处理
    - 支持打断当前播放
    """
    
    def __init__(self, context: Context):
        super().__init__(context)
        self.voice_clients: Dict[int, VoiceClient] = {}
        self.audio_queues: Dict[int, asyncio.Queue] = {}
        self.queue_tasks: Dict[int, asyncio.Task] = {}
        self.is_playing: Dict[int, bool] = defaultdict(bool)
        self.current_tts_task: Dict[int, Optional[asyncio.Task]] = defaultdict(lambda: None)
        
        # TTS 配置
        self.tts_enabled_guilds: set = set()
        # edge-tts 中文语音
        self.tts_voice_zh = "zh-CN-XiaoxiaoNeural"
        # VoiceVox 日语语音 (speaker ID, 2 = ずんだもん)
        self.tts_voice_ja_id = 2
        self.tts_rate = "+0%"
        self.tts_volume = "+0%"
        # VoiceVox API endpoint (default: local server)
        self.voicevox_url = "http://localhost:50021"
        self.tts_voicevox_speed = 1.0  # VoiceVox 语速
        self.tts_voicevox_pitch = 0.0  # VoiceVox 音调
        
        # 临时目录用于存储音频文件
        self.temp_dir = Path(tempfile.gettempdir()) / "astrbot_tts"
        self.temp_dir.mkdir(exist_ok=True)
        
    async def initialize(self):
        """插件初始化"""
        logger.info("Discord Voice TTS 插件已初始化")
        
        # 检查依赖
        if not EDGE_TTS_AVAILABLE:
            logger.warning("edge-tts 未安装，中文 TTS 不可用。请运行: pip install edge-tts")
        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp 未安装，VoiceVox 日语 TTS 不可用。请运行: pip install aiohttp")
        if not EDGE_TTS_AVAILABLE and not AIOHTTP_AVAILABLE and not ASTRBOT_TTS_AVAILABLE:
            logger.error("没有可用的 TTS 引擎，请安装 edge-tts 和/或 aiohttp")
            
    async def terminate(self):
        """插件销毁时清理资源"""
        logger.info("正在清理 Discord Voice TTS 资源...")
        
        # 断开所有语音连接
        for guild_id, voice_client in list(self.voice_clients.items()):
            try:
                if voice_client.is_connected():
                    await voice_client.disconnect()
            except Exception as e:
                logger.error(f"断开语音连接失败: {e}")
                
        # 取消所有队列任务
        for guild_id, task in list(self.queue_tasks.items()):
            if not task.done():
                task.cancel()
                
        # 清理临时文件
        try:
            for file in self.temp_dir.glob("*.mp3"):
                file.unlink(missing_ok=True)
            for file in self.temp_dir.glob("*.wav"):
                file.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"清理临时文件失败: {e}")
            
        logger.info("Discord Voice TTS 资源已清理")
        
    async def generate_tts_audio(self, text: str, guild_id: int) -> Optional[Path]:
        """生成 TTS 音频文件
        
        自动检测语言：
          - 包含日文假名 → VoiceVox-TTS (日语语音)
          - 其他 (中文/英文等) → edge-tts (中文语音)
        
        Args:
            text: 要转换的文本
            guild_id: 服务器 ID，用于生成唯一文件名
            
        Returns:
            音频文件路径，失败返回 None
        """
        try:
            lang = detect_language(text)
            
            if lang == 'ja':
                return await self._generate_voicevox_audio(text, guild_id)
            else:
                return await self._generate_edge_tts_audio(text, guild_id)
                
        except Exception as e:
            logger.error(f"生成 TTS 音频失败: {e}")
            return None

    async def _generate_edge_tts_audio(self, text: str, guild_id: int) -> Optional[Path]:
        """使用 edge-tts 生成中文音频
        
        Args:
            text: 要转换的文本
            guild_id: 服务器 ID
            
        Returns:
            音频文件路径，失败返回 None
        """
        try:
            if not EDGE_TTS_AVAILABLE:
                logger.error("edge-tts 未安装，无法生成中文语音")
                return None
                
            audio_file = self.temp_dir / f"tts_zh_{guild_id}_{hash(text)}.mp3"
            
            if audio_file.exists():
                return audio_file
                
            communicate = edge_tts.Communicate(
                text,
                voice=self.tts_voice_zh,
                rate=self.tts_rate,
                volume=self.tts_volume
            )
            await communicate.save(str(audio_file))
            logger.info(f"[edge-tts] 生成中文音频: {audio_file}")
            return audio_file
            
        except Exception as e:
            logger.error(f"edge-tts 生成音频失败: {e}")
            return None

    async def _generate_voicevox_audio(self, text: str, guild_id: int) -> Optional[Path]:
        """使用 VoiceVox HTTP API 生成日语音频
        
        需要 VoiceVox 引擎在本地运行 (默认端口 50021)。
        
        Args:
            text: 日语文本
            guild_id: 服务器 ID
            
        Returns:
            音频文件路径 (WAV)，失败返回 None
        """
        try:
            if not AIOHTTP_AVAILABLE:
                logger.error("aiohttp 未安装，无法使用 VoiceVox TTS")
                return None
                
            audio_file = self.temp_dir / f"tts_ja_{guild_id}_{hash(text)}.wav"
            
            if audio_file.exists():
                return audio_file
                
            base_url = self.voicevox_url.rstrip('/')
            speaker_id = self.tts_voice_ja_id
            
            async with aiohttp.ClientSession() as session:
                # Step 1: 生成 audio_query
                params = {"text": text, "speaker": speaker_id}
                async with session.post(
                    f"{base_url}/audio_query",
                    params=params
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"VoiceVox audio_query 失败 ({resp.status}): {body}")
                        return None
                    query_data = await resp.json()
                    
                # 应用语速与音调设置
                query_data["speedScale"] = self.tts_voicevox_speed
                query_data["pitchScale"] = self.tts_voicevox_pitch
                
                # Step 2: 合成音频
                async with session.post(
                    f"{base_url}/synthesis",
                    params={"speaker": speaker_id},
                    json=query_data,
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"VoiceVox synthesis 失败 ({resp.status}): {body}")
                        return None
                    wav_data = await resp.read()
                    
            with open(audio_file, "wb") as f:
                f.write(wav_data)
            logger.info(f"[VoiceVox] 生成日语音频: {audio_file} (speaker={speaker_id})")
            return audio_file
            
        except aiohttp.ClientConnectorError:
            logger.error(
                f"无法连接到 VoiceVox 引擎 ({self.voicevox_url})，"
                "请确认 VoiceVox 已启动并监听该端口"
            )
            return None
        except Exception as e:
            logger.error(f"VoiceVox 生成音频失败: {e}")
            return None
            
    async def play_audio_queue(self, guild_id: int):
        """播放音频队列
        
        Args:
            guild_id: 服务器 ID
        """
        voice_client = self.voice_clients.get(guild_id)
        if not voice_client:
            return
            
        queue = self.audio_queues.get(guild_id)
        if not queue:
            return
            
        while True:
            try:
                # 等待队列中的音频文件
                audio_file = await queue.get()
                
                if audio_file is None:
                    # None 表示停止信号
                    break
                    
                if not voice_client.is_connected():
                    logger.warning("语音客户端已断开，停止播放队列")
                    break
                    
                # 检查文件是否存在
                if not audio_file.exists():
                    logger.warning(f"音频文件不存在: {audio_file}")
                    queue.task_done()
                    continue
                    
                # 播放音频
                self.is_playing[guild_id] = True
                logger.info(f"开始播放音频: {audio_file}")
                
                # 使用 FFmpegPCMAudio 播放
                audio_source = discord.FFmpegPCMAudio(
                    str(audio_file),
                    options="-vn"  # 不处理视频
                )
                
                # 如果需要音量控制，使用 PCMVolumeTransformer
                # audio_source = discord.PCMVolumeTransformer(audio_source, volume=1.0)
                
                voice_client.play(audio_source)
                
                # 等待播放完成
                while voice_client.is_playing():
                    await asyncio.sleep(0.1)
                    
                self.is_playing[guild_id] = False
                logger.info(f"音频播放完成: {audio_file}")
                
                # 清理临时文件（可选）
                # audio_file.unlink(missing_ok=True)
                
                queue.task_done()
                
            except asyncio.CancelledError:
                logger.info("音频队列任务被取消")
                break
            except Exception as e:
                logger.error(f"播放音频时出错: {e}")
                self.is_playing[guild_id] = False
                queue.task_done()
                
    async def connect_to_voice_channel(self, channel: VoiceChannel) -> Optional[VoiceClient]:
        """连接到语音频道
        
        Args:
            channel: 语音频道
            
        Returns:
            VoiceClient 对象，失败返回 None
        """
        try:
            guild_id = channel.guild.id
            
            # 检查是否已连接
            if guild_id in self.voice_clients:
                voice_client = self.voice_clients[guild_id]
                if voice_client.is_connected():
                    # 如果已在同一频道，直接返回
                    if voice_client.channel.id == channel.id:
                        return voice_client
                    # 如果在不同频道，先断开
                    await voice_client.disconnect()
                    
            # 连接到语音频道
            voice_client = await channel.connect()
            self.voice_clients[guild_id] = voice_client
            
            # 初始化音频队列
            if guild_id not in self.audio_queues:
                self.audio_queues[guild_id] = asyncio.Queue()
                
            # 启动播放队列任务
            if guild_id not in self.queue_tasks or self.queue_tasks[guild_id].done():
                self.queue_tasks[guild_id] = asyncio.create_task(
                    self.play_audio_queue(guild_id)
                )
                
            logger.info(f"已连接到语音频道: {channel.name} (服务器: {channel.guild.name})")
            return voice_client
            
        except Exception as e:
            logger.error(f"连接语音频道失败: {e}")
            return None
            
    async def disconnect_from_voice_channel(self, guild_id: int):
        """断开语音频道连接
        
        Args:
            guild_id: 服务器 ID
        """
        try:
            if guild_id in self.voice_clients:
                voice_client = self.voice_clients[guild_id]
                
                # 停止当前播放
                if voice_client.is_playing():
                    voice_client.stop()
                    
                # 发送停止信号到队列
                if guild_id in self.audio_queues:
                    await self.audio_queues[guild_id].put(None)
                    
                # 等待队列任务完成
                if guild_id in self.queue_tasks:
                    task = self.queue_tasks[guild_id]
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                            
                # 断开连接
                if voice_client.is_connected():
                    await voice_client.disconnect()
                    
                # 清理资源
                del self.voice_clients[guild_id]
                if guild_id in self.audio_queues:
                    del self.audio_queues[guild_id]
                if guild_id in self.queue_tasks:
                    del self.queue_tasks[guild_id]
                    
                logger.info(f"已断开语音频道连接 (服务器 ID: {guild_id})")
                
        except Exception as e:
            logger.error(f"断开语音频道连接失败: {e}")
            
    async def interrupt_current_playback(self, guild_id: int):
        """打断当前播放
        
        Args:
            guild_id: 服务器 ID
        """
        try:
            if guild_id in self.voice_clients:
                voice_client = self.voice_clients[guild_id]
                if voice_client.is_playing():
                    voice_client.stop()
                    logger.info(f"已打断当前播放 (服务器 ID: {guild_id})")
                    
        except Exception as e:
            logger.error(f"打断播放失败: {e}")
            
    @filter.command("dv_tts_join")
    async def join_voice_channel(self, event: AstrMessageEvent):
        """加入语音频道
        
        用法: /dv_tts_join
        需要用户在语音频道中
        """
        try:
            # 获取用户所在的语音频道
            guild = event.message_obj.guild
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return
                
            # 获取用户
            user = event.message_obj.author
            if not user:
                yield event.plain_result("无法获取用户信息")
                return
                
            # 获取用户的语音状态
            member = guild.get_member(user.id)
            if not member or not member.voice:
                yield event.plain_result("您需要先加入一个语音频道")
                return
                
            voice_channel = member.voice.channel
            if not voice_channel:
                yield event.plain_result("您需要先加入一个语音频道")
                return
                
            # 连接到语音频道
            voice_client = await self.connect_to_voice_channel(voice_channel)
            if voice_client:
                yield event.plain_result(f"已加入语音频道: {voice_channel.name}")
            else:
                yield event.plain_result("加入语音频道失败")
                
        except Exception as e:
            logger.error(f"加入语音频道失败: {e}")
            yield event.plain_result(f"加入语音频道失败: {str(e)}")
            
    @filter.command("dv_tts_leave")
    async def leave_voice_channel(self, event: AstrMessageEvent):
        """离开语音频道
        
        用法: /dv_tts_leave
        """
        try:
            guild = event.message_obj.guild
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return
                
            guild_id = guild.id
            
            if guild_id not in self.voice_clients:
                yield event.plain_result("当前未连接到任何语音频道")
                return
                
            await self.disconnect_from_voice_channel(guild_id)
            yield event.plain_result("已离开语音频道")
            
        except Exception as e:
            logger.error(f"离开语音频道失败: {e}")
            yield event.plain_result(f"离开语音频道失败: {str(e)}")
            
    @filter.command("dv_tts")
    async def tts_command(self, event: AstrMessageEvent):
        """TTS 语音合成命令
        
        用法: /dv_tts <文本>
        将文本转换为语音并播放（自动检测中文/日语）
        """
        try:
            # 获取文本
            text = event.message_str
            if not text:
                yield event.plain_result("请提供要转换的文本")
                return
                
            # 移除命令部分
            if text.startswith("/dv_tts"):
                text = text[7:].strip()
                
            if not text:
                yield event.plain_result("请提供要转换的文本")
                return
                
            # 获取服务器信息
            channel = event.message_obj.channel
            if not channel:
                yield event.plain_result("无法获取频道信息")
                return
            guild = getattr(channel, 'guild', None) or getattr(channel, 'parent', None)
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return
                
            guild_id = guild.id
            
            # 检查是否已连接到语音频道
            if guild_id not in self.voice_clients:
                # 尝试自动加入用户所在的语音频道
                user = event.message_obj.author
                member = guild.get_member(user.id)
                if member and member.voice and member.voice.channel:
                    voice_client = await self.connect_to_voice_channel(member.voice.channel)
                    if not voice_client:
                        yield event.plain_result("无法加入语音频道，请先使用 /tts_join 命令")
                        return
                else:
                    yield event.plain_result("请先使用 /tts_join 命令加入语音频道")
                    return
                    
            # 生成 TTS 音频
            audio_file = await self.generate_tts_audio(text, guild_id)
            if not audio_file:
                yield event.plain_result("生成语音失败")
                return
                
            # 添加到播放队列
            if guild_id in self.audio_queues:
                await self.audio_queues[guild_id].put(audio_file)
                yield event.plain_result(f"已添加到播放队列: {text[:50]}...")
            else:
                yield event.plain_result("播放队列未初始化")
                
        except Exception as e:
            logger.error(f"TTS 命令执行失败: {e}")
            yield event.plain_result(f"TTS 命令执行失败: {str(e)}")
            
    @filter.command("dv_tts_stop")
    async def stop_tts(self, event: AstrMessageEvent):
        """停止当前 TTS 播放
        
        用法: /dv_tts_stop
        """
        try:
            guild = event.message_obj.guild
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return
                
            guild_id = guild.id
            
            if guild_id not in self.voice_clients:
                yield event.plain_result("当前未连接到任何语音频道")
                return
                
            # 打断当前播放
            await self.interrupt_current_playback(guild_id)
            
            # 清空队列
            if guild_id in self.audio_queues:
                while not self.audio_queues[guild_id].empty():
                    try:
                        self.audio_queues[guild_id].get_nowait()
                    except asyncio.QueueEmpty:
                        break
                        
            yield event.plain_result("已停止 TTS 播放")
            
        except Exception as e:
            logger.error(f"停止 TTS 失败: {e}")
            yield event.plain_result(f"停止 TTS 失败: {str(e)}")
            
    @filter.command("dv_tts_config")
    async def config_tts(self, event: AstrMessageEvent):
        """配置 TTS 设置

        用法:
          /dv_tts_config voice_zh <edge-tts 语音名>  - 设置中文语音 (edge-tts)
          /dv_tts_config voice_ja <VoiceVox Speaker ID>  - 设置日语语音 (VoiceVox, 整数 ID)
          /dv_tts_config rate <语速>  - edge-tts 语速 (如: +20%)
          /dv_tts_config volume <音量>  - edge-tts 音量 (如: +10%)
          /dv_tts_config voicevox_url <URL>  - VoiceVox 引擎地址 (默认 http://localhost:50021)
          /dv_tts_config voicevox_speed <速度>  - VoiceVox 语速倍率 (浮点数, 默认 1.0)
          /dv_tts_config voicevox_pitch <音调>  - VoiceVox 音调偏移 (浮点数, 默认 0.0)
        """
        try:
            message_str = event.message_str
            parts = message_str.split()
            
            if len(parts) < 3:
                yield event.plain_result(
                    "用法:\n"
                    "/dv_tts_config voice_zh <edge-tts 语音名> - 设置中文语音 (edge-tts)\n"
                    "/dv_tts_config voice_ja <Speaker ID> - 设置日语 VoiceVox 说话人 ID (整数)\n"
                    "/dv_tts_config rate <语速> - edge-tts 语速 (如: +20%)\n"
                    "/dv_tts_config volume <音量> - edge-tts 音量 (如: +10%)\n"
                    "/dv_tts_config voicevox_url <URL> - VoiceVox 引擎地址\n"
                    "/dv_tts_config voicevox_speed <倍率> - VoiceVox 语速 (如: 1.2)\n"
                    "/dv_tts_config voicevox_pitch <偏移> - VoiceVox 音调 (如: 0.05)"
                )
                return
                
            config_type = parts[1].lower()
            config_value = parts[2]
            
            if config_type == "voice_zh":
                self.tts_voice_zh = config_value
                yield event.plain_result(f"中文语音 (edge-tts) 已设置为: {config_value}")
            elif config_type == "voice_ja":
                try:
                    self.tts_voice_ja_id = int(config_value)
                    yield event.plain_result(f"日语 VoiceVox 说话人 ID 已设置为: {config_value}")
                except ValueError:
                    yield event.plain_result("VoiceVox Speaker ID 必须为整数，例如: /dv_tts_config voice_ja 2")
            elif config_type == "rate":
                self.tts_rate = config_value
                yield event.plain_result(f"edge-tts 语速已设置为: {config_value}")
            elif config_type == "volume":
                self.tts_volume = config_value
                yield event.plain_result(f"edge-tts 音量已设置为: {config_value}")
            elif config_type == "voicevox_url":
                self.voicevox_url = config_value
                yield event.plain_result(f"VoiceVox 引擎地址已设置为: {config_value}")
            elif config_type == "voicevox_speed":
                try:
                    self.tts_voicevox_speed = float(config_value)
                    yield event.plain_result(f"VoiceVox 语速已设置为: {config_value}")
                except ValueError:
                    yield event.plain_result("VoiceVox 语速必须为浮点数，例如: /dv_tts_config voicevox_speed 1.2")
            elif config_type == "voicevox_pitch":
                try:
                    self.tts_voicevox_pitch = float(config_value)
                    yield event.plain_result(f"VoiceVox 音调已设置为: {config_value}")
                except ValueError:
                    yield event.plain_result("VoiceVox 音调必须为浮点数，例如: /dv_tts_config voicevox_pitch 0.05")
            else:
                yield event.plain_result(
                    f"未知的配置类型: {config_type}\n"
                    "可用类型: voice_zh, voice_ja, rate, volume, voicevox_url, voicevox_speed, voicevox_pitch"
                )
                
        except Exception as e:
            logger.error(f"配置 TTS 失败: {e}")
            yield event.plain_result(f"配置 TTS 失败: {str(e)}")
            
    @filter.command("dv_tts_status")
    async def tts_status(self, event: AstrMessageEvent):
        """查看 TTS 状态
        
        用法: /dv_tts_status
        """
        try:
            guild = event.message_obj.guild
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return
                
            guild_id = guild.id
            
            status_lines = []
            status_lines.append("=== TTS 状态 ===")
            
            # 连接状态
            if guild_id in self.voice_clients:
                voice_client = self.voice_clients[guild_id]
                if voice_client.is_connected():
                    status_lines.append(f"语音频道: {voice_client.channel.name}")
                    status_lines.append(f"播放状态: {'播放中' if self.is_playing[guild_id] else '空闲'}")
                else:
                    status_lines.append("语音连接: 已断开")
            else:
                status_lines.append("语音连接: 未连接")
                
            # 队列状态
            if guild_id in self.audio_queues:
                queue_size = self.audio_queues[guild_id].qsize()
                status_lines.append(f"队列长度: {queue_size}")
            else:
                status_lines.append("队列状态: 未初始化")
                
            # TTS 配置
            status_lines.append(f"中文语音 (edge-tts): {self.tts_voice_zh}")
            status_lines.append(f"日语 VoiceVox Speaker ID: {self.tts_voice_ja_id}")
            status_lines.append(f"edge-tts 语速: {self.tts_rate}")
            status_lines.append(f"edge-tts 音量: {self.tts_volume}")
            status_lines.append(f"VoiceVox 引擎地址: {self.voicevox_url}")
            status_lines.append(f"VoiceVox 语速倍率: {self.tts_voicevox_speed}")
            status_lines.append(f"VoiceVox 音调偏移: {self.tts_voicevox_pitch}")
            
            # TTS 引擎状态
            engines = []
            if EDGE_TTS_AVAILABLE:
                engines.append("edge-tts (中文)")
            if AIOHTTP_AVAILABLE:
                engines.append("VoiceVox HTTP (日语)")
            if ASTRBOT_TTS_AVAILABLE:
                engines.append("AstrBot TTS")
            status_lines.append(f"可用 TTS 引擎: {', '.join(engines) if engines else '无'}")
                
            yield event.plain_result("\n".join(status_lines))
            
        except Exception as e:
            logger.error(f"获取 TTS 状态失败: {e}")
            yield event.plain_result(f"获取 TTS 状态失败: {str(e)}")
            
    @filter.command("dv_tts_help")
    async def tts_help(self, event: AstrMessageEvent):
        """显示 TTS 帮助信息
        
        用法: /dv_tts_help
        """
        help_text = """
=== Discord 语音频道 TTS 插件 ===

命令列表:
/dv_tts_join - 加入语音频道 (需要先在语音频道中)
/dv_tts_leave - 离开语音频道
/dv_tts <文本> - 将文本转换为语音并播放 (自动检测中/日语)
/dv_tts_stop - 停止当前 TTS 播放
/dv_tts_config voice_zh <语音名> - 设置中文语音 (edge-tts)
/dv_tts_config voice_ja <Speaker ID> - 设置日语说话人 ID (VoiceVox)
/dv_tts_config rate <语速> - edge-tts 语速 (如: +20%)
/dv_tts_config volume <音量> - edge-tts 音量 (如: +10%)
/dv_tts_config voicevox_url <URL> - VoiceVox 引擎地址
/dv_tts_config voicevox_speed <倍率> - VoiceVox 语速 (如: 1.2)
/dv_tts_config voicevox_pitch <偏移> - VoiceVox 音调 (如: 0.05)
/dv_tts_status - 查看 TTS 状态
/dv_tts_help - 显示此帮助信息

TTS 引擎说明:
  • 中文/其他语言 → edge-tts (微软神经网络 TTS)
  • 日语 (含假名) → VoiceVox HTTP API (本地引擎)

示例:
/dv_tts_join
/dv_tts 你好，世界！
/dv_tts こんにちは！
/dv_tts_config voice_zh zh-CN-YunxiNeural
/dv_tts_config voice_ja 3
/dv_tts_config rate +10%
/dv_tts_config voicevox_url http://localhost:50021
/dv_tts_config voicevox_speed 1.2
/dv_tts_stop
/dv_tts_leave

支持的中文语音 (edge-tts):
- zh-CN-XiaoxiaoNeural (女声，温柔)
- zh-CN-YunxiNeural (男声，活泼)
- zh-CN-YunjianNeural (男声，沉稳)
- zh-CN-XiaoyiNeural (女声，甜美)
- en-US-AriaNeural (英文女声)
- en-US-GuyNeural (英文男声)

常用 VoiceVox Speaker ID:
- 2  ずんだもん (默认)
- 3  春日部つむぎ
- 8  春日部つむぎ (ノーマル)
- 13 青山龍星
  (完整列表请访问 VoiceVox 官网或调用 /speakers API)

注意事项:
1. 需要安装 edge-tts: pip install edge-tts
2. 需要安装 aiohttp: pip install aiohttp
3. 日语 TTS 需要本地运行 VoiceVox 引擎 (https://voicevox.hiroshiba.jp/)
4. 需要安装 ffmpeg 并添加到系统 PATH
5. 需要安装 pynacl: pip install pynacl
"""
        yield event.plain_result(help_text)
        
    # 监听消息事件，自动 TTS (可选功能)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听消息事件，自动 TTS (可选功能)
        
        注意：此功能默认禁用，需要手动启用
        """
        # 这里可以实现自动 TTS 功能
        # 例如：监听特定频道的消息，自动转换为语音
        # 为了性能考虑，默认禁用此功能
        pass
