import asyncio
import os
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
    logger.warning("edge-tts not installed. TTS functionality will be limited.")

try:
    from astrbot.api import tts as astrbot_tts
    ASTRBOT_TTS_AVAILABLE = True
except ImportError:
    ASTRBOT_TTS_AVAILABLE = False
    logger.warning("AstrBot TTS module not available.")


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
        self.tts_voice = "zh-CN-XiaoxiaoNeural"  # 默认中文语音
        self.tts_rate = "+0%"
        self.tts_volume = "+0%"
        
        # 临时目录用于存储音频文件
        self.temp_dir = Path(tempfile.gettempdir()) / "astrbot_tts"
        self.temp_dir.mkdir(exist_ok=True)
        
    async def initialize(self):
        """插件初始化"""
        logger.info("Discord Voice TTS 插件已初始化")
        
        # 检查依赖
        if not EDGE_TTS_AVAILABLE and not ASTRBOT_TTS_AVAILABLE:
            logger.error("没有可用的 TTS 引擎，请安装 edge-tts 或确保 AstrBot TTS 模块可用")
            
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
        
        Args:
            text: 要转换的文本
            guild_id: 服务器 ID，用于生成唯一文件名
            
        Returns:
            音频文件路径，失败返回 None
        """
        try:
            # 生成唯一文件名
            audio_file = self.temp_dir / f"tts_{guild_id}_{hash(text)}.mp3"
            
            # 如果文件已存在，直接返回
            if audio_file.exists():
                return audio_file
                
            # 优先使用 edge-tts
            if EDGE_TTS_AVAILABLE:
                communicate = edge_tts.Communicate(
                    text,
                    voice=self.tts_voice,
                    rate=self.tts_rate,
                    volume=self.tts_volume
                )
                await communicate.save(str(audio_file))
                logger.info(f"使用 edge-tts 生成音频: {audio_file}")
                return audio_file
                
            # 备选使用 AstrBot TTS
            elif ASTRBOT_TTS_AVAILABLE:
                # 这里需要根据 AstrBot TTS 模块的实际 API 进行调整
                # 假设有一个通用的 tts 方法
                try:
                    # 尝试使用 AstrBot TTS
                    # 注意：这里需要根据实际的 AstrBot TTS API 进行调整
                    audio_data = await astrbot_tts.generate(text)
                    if audio_data:
                        with open(audio_file, "wb") as f:
                            f.write(audio_data)
                        logger.info(f"使用 AstrBot TTS 生成音频: {audio_file}")
                        return audio_file
                except Exception as e:
                    logger.error(f"AstrBot TTS 生成失败: {e}")
                    
            logger.error("没有可用的 TTS 引擎")
            return None
            
        except Exception as e:
            logger.error(f"生成 TTS 音频失败: {e}")
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
            
    @filter.command("tts_join")
    async def join_voice_channel(self, event: AstrMessageEvent):
        """加入语音频道
        
        用法: /tts_join
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
            
    @filter.command("tts_leave")
    async def leave_voice_channel(self, event: AstrMessageEvent):
        """离开语音频道
        
        用法: /tts_leave
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
            
    @filter.command("tts")
    async def tts_command(self, event: AstrMessageEvent):
        """TTS 命令
        
        用法: /tts <文本>
        将文本转换为语音并播放
        """
        try:
            # 获取文本
            text = event.message_str
            if not text:
                yield event.plain_result("请提供要转换的文本")
                return
                
            # 移除命令部分
            if text.startswith("/tts"):
                text = text[4:].strip()
                
            if not text:
                yield event.plain_result("请提供要转换的文本")
                return
                
            # 获取服务器信息
            guild = event.message_obj.guild
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
            
    @filter.command("tts_stop")
    async def stop_tts(self, event: AstrMessageEvent):
        """停止当前 TTS 播放
        
        用法: /tts_stop
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
            
    @filter.command("tts_config")
    async def config_tts(self, event: AstrMessageEvent):
        """配置 TTS 设置
        
        用法: /tts_config voice <语音名称>
              /tts_config rate <语速>
              /tts_config volume <音量>
        """
        try:
            message_str = event.message_str
            parts = message_str.split()
            
            if len(parts) < 3:
                yield event.plain_result(
                    "用法:\n"
                    "/tts_config voice <语音名称> - 设置语音\n"
                    "/tts_config rate <语速> - 设置语速 (如: +20%)\n"
                    "/tts_config volume <音量> - 设置音量 (如: +10%)"
                )
                return
                
            config_type = parts[1].lower()
            config_value = parts[2]
            
            if config_type == "voice":
                self.tts_voice = config_value
                yield event.plain_result(f"语音已设置为: {config_value}")
            elif config_type == "rate":
                self.tts_rate = config_value
                yield event.plain_result(f"语速已设置为: {config_value}")
            elif config_type == "volume":
                self.tts_volume = config_value
                yield event.plain_result(f"音量已设置为: {config_value}")
            else:
                yield event.plain_result("未知的配置类型")
                
        except Exception as e:
            logger.error(f"配置 TTS 失败: {e}")
            yield event.plain_result(f"配置 TTS 失败: {str(e)}")
            
    @filter.command("tts_status")
    async def tts_status(self, event: AstrMessageEvent):
        """查看 TTS 状态
        
        用法: /tts_status
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
            status_lines.append(f"当前语音: {self.tts_voice}")
            status_lines.append(f"当前语速: {self.tts_rate}")
            status_lines.append(f"当前音量: {self.tts_volume}")
            
            # TTS 引擎状态
            if EDGE_TTS_AVAILABLE:
                status_lines.append("TTS 引擎: edge-tts")
            elif ASTRBOT_TTS_AVAILABLE:
                status_lines.append("TTS 引擎: AstrBot TTS")
            else:
                status_lines.append("TTS 引擎: 无可用引擎")
                
            yield event.plain_result("\n".join(status_lines))
            
        except Exception as e:
            logger.error(f"获取 TTS 状态失败: {e}")
            yield event.plain_result(f"获取 TTS 状态失败: {str(e)}")
            
    @filter.command("tts_help")
    async def tts_help(self, event: AstrMessageEvent):
        """显示 TTS 帮助信息
        
        用法: /tts_help
        """
        help_text = """
=== Discord 语音频道 TTS 插件 ===

命令列表:
/tts_join - 加入语音频道 (需要先在语音频道中)
/tts_leave - 离开语音频道
/tts <文本> - 将文本转换为语音并播放
/tts_stop - 停止当前 TTS 播放
/tts_config voice <语音名称> - 设置语音
/tts_config rate <语速> - 设置语速 (如: +20%)
/tts_config volume <音量> - 设置音量 (如: +10%)
/tts_status - 查看 TTS 状态
/tts_help - 显示此帮助信息

示例:
/tts_join
/tts 你好，世界！
/tts_config voice zh-CN-YunxiNeural
/tts_config rate +10%
/tts_stop
/tts_leave

支持的语音 (edge-tts):
- zh-CN-XiaoxiaoNeural (女声，温柔)
- zh-CN-YunxiNeural (男声，活泼)
- zh-CN-YunjianNeural (男声，沉稳)
- zh-CN-XiaoyiNeural (女声，甜美)
- en-US-AriaNeural (英文女声)
- en-US-GuyNeural (英文男声)

注意事项:
1. 需要安装 edge-tts: pip install edge-tts
2. 需要安装 ffmpeg 并添加到系统 PATH
3. 需要安装 pynacl: pip install pynacl
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
