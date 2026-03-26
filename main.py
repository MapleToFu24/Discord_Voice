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
        logger.debug(f"[DiscordVoiceTTS] Plugin context: {self.context}")
        logger.debug(f"[DiscordVoiceTTS] TTS config - edge_tts: {EDGE_TTS_AVAILABLE}, aiohttp: {AIOHTTP_AVAILABLE}, astrbot_tts: {ASTRBOT_TTS_AVAILABLE}")
        
        # 检查依赖
        if not EDGE_TTS_AVAILABLE:
            logger.warning("edge-tts 未安装，中文 TTS 不可用。请运行: pip install edge-tts")
        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp 未安装，VoiceVox 日语 TTS 不可用。请运行: pip install aiohttp")
        if not EDGE_TTS_AVAILABLE and not AIOHTTP_AVAILABLE and not ASTRBOT_TTS_AVAILABLE:
            logger.error("没有可用的 TTS 引擎，请安装 edge-tts 和/或 aiohttp")

    def _get_discord_bot_client(self):
        """通过 AstrBot context 遍历平台适配器，获取 Discord bot/client 实例"""
        try:
            for platform in self.context.platform_insts:
                platform_type = str(type(platform)).lower()
                if 'discord' not in platform_type:
                    continue
                for attr in ['client', 'bot', '_client', '_bot', 'discord_client']:
                    client = getattr(platform, attr, None)
                    if client and hasattr(client, 'guilds'):
                        return client
        except Exception as e:
            logger.error(f"[DiscordVoiceTTS] 获取 Discord bot client 失败: {e}")
        return None

    async def _resolve_guild_and_author(self, event: AstrMessageEvent):
        """
        尝试多种途径获取 discord.Guild 与 discord.Member。
        
        返回: (guild, author) 或 (None, None)
        """
        guild = None
        author = None

        # --- 途径 0: 直接从 event 获取原生 Discord 消息对象 ---
        try:
            raw = getattr(event, 'raw_message', None) or event.get_extra("raw_message")
            if raw and hasattr(raw, 'guild') and hasattr(raw, 'author'):
                guild = getattr(raw, 'guild', None)
                author = getattr(raw, 'author', None)
                if guild is not None and author is not None:
                    logger.debug(f"[resolve] 途径0 (event.raw_message) guild={guild.name}, author={author.name}")
                    return guild, author
        except Exception as e:
            logger.debug(f"[resolve] 途径0 失败: {e}")

        # --- 途径 1: message_obj 本身就是原生 discord.Message ---
        msg = event.message_obj
        guild  = getattr(msg, 'guild',  None)
        author = getattr(msg, 'author', None)
        logger.debug(f"[resolve] 途径1 guild={guild}, author={author}")

        # --- 途径 2: message_obj 包了一层 raw_message ---
        if guild is None:
            raw = getattr(msg, 'raw_message', None)
            if raw:
                guild  = getattr(raw, 'guild',  None)
                author = getattr(raw, 'author', None) or author
                logger.debug(f"[resolve] 途径2 (raw_message) guild={guild}, author={author}")

        # --- 途径 3: 通过 channel → guild ---
        if guild is None:
            for obj in [msg, getattr(msg, 'raw_message', None)]:
                if obj is None:
                    continue
                channel = getattr(obj, 'channel', None)
                if channel:
                    guild = getattr(channel, 'guild', None)
                    if guild:
                        logger.debug(f"[resolve] 途径3 (channel.guild) guild={guild}")
                        break

        # --- 途径 4: 通过 Discord bot client + channel_id 查找 ---
        if guild is None:
            bot = self._get_discord_bot_client()
            if bot:
                # 尝试从各种属性中取 channel_id
                raw = getattr(msg, 'raw_message', None)
                channel_id = (
                    getattr(msg, 'channel_id', None)
                    or getattr(raw, 'channel_id', None)
                    or (getattr(getattr(msg, 'channel', None), 'id', None))
                    or (getattr(getattr(raw, 'channel', None), 'id', None) if raw else None)
                )
                if channel_id:
                    ch = bot.get_channel(int(channel_id))
                    if ch:
                        guild = getattr(ch, 'guild', None)
                        logger.debug(f"[resolve] 途径4 (bot.get_channel) channel_id={channel_id}, guild={guild}")

        if guild is None:
            logger.warning(
                f"[resolve] 所有途径均无法获取 guild。\n"
                f"  message_obj type  : {type(msg)}\n"
                f"  message_obj attrs : {[a for a in dir(msg) if not a.startswith('__')]}"
            )

        return guild, author
            
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
            

                
    async def connect_to_voice_channel(self, channel):
        try:
            guild = channel.guild
            guild_id = guild.id

            # ── 步骤 1：优先检查 discord.py 内部的 guild.voice_client ──
            # 这能捕获"本地字典没有、但 discord.py 内部已连接"的残留状态
            existing_vc = guild.voice_client

            if existing_vc is not None:
                if existing_vc.is_connected():
                    if existing_vc.channel.id == channel.id:
                        # 已在目标频道，直接复用，同步进本地字典
                        logger.info(f"[connect] 复用已有连接: {channel.name}")
                        self.voice_clients[guild_id] = existing_vc
                        if guild_id not in self.audio_queues:
                            self.audio_queues[guild_id] = asyncio.Queue()
                        if guild_id not in self.is_playing:
                            self.is_playing[guild_id] = False
                            asyncio.create_task(self._play_queue_loop(guild_id))
                            asyncio.create_task(self._keepalive_loop(guild_id))
                        return existing_vc
                    else:
                        # 已连接但在其他频道，强制断开再重连
                        logger.info(f"[connect] 已连接到其他频道 ({existing_vc.channel.name})，断开后重连")
                        try:
                            await existing_vc.disconnect(force=True)
                        except Exception as e:
                            logger.warning(f"[connect] 断开旧连接时出错（忽略）: {e}")
                        # 确保 guild.voice_client 被清除，防止残留状态
                        guild._voice_client = None
                else:
                    # 对象存在但已断线，强制清理
                    logger.warning(f"[connect] 发现已断线的残留 voice_client，强制清理")
                    try:
                        await existing_vc.disconnect(force=True)
                    except Exception:
                        pass

            # ── 步骤 2：清理本地字典中的旧记录 ──
            old_vc = self.voice_clients.pop(guild_id, None)
            if old_vc is not None and old_vc is not existing_vc:
                try:
                    await old_vc.disconnect(force=True)
                except Exception:
                    pass

            # 等待 discord.py 内部状态完全释放
            await asyncio.sleep(0.5)

            # ── 步骤 3：重试连接，最多 3 次 ──
            for attempt in range(1, 4):
                try:
                    logger.info(f"[connect] 尝试连接语音频道: {channel.name} (第 {attempt} 次)")
                    voice_client = await channel.connect(timeout=30.0, reconnect=True)

                    # 等待 WebSocket 握手稳定
                    for _ in range(10):
                        await asyncio.sleep(0.3)
                        if voice_client.is_connected():
                            break
                    else:
                        logger.warning(f"[connect] 连接后 is_connected() 仍为 False，重试...")
                        try:
                            await voice_client.disconnect(force=True)
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                        continue

                    self.voice_clients[guild_id] = voice_client

                    if guild_id not in self.audio_queues:
                        self.audio_queues[guild_id] = asyncio.Queue()
                    self.is_playing[guild_id] = False

                    asyncio.create_task(self._play_queue_loop(guild_id))
                    asyncio.create_task(self._keepalive_loop(guild_id))

                    logger.info(f"[connect] 已成功连接: {channel.name} (服务器: {guild.name})")
                    return voice_client

                except discord.ClientException as e:
                    # 捕获 "Already connected" 等 discord.py 级别的异常
                    logger.error(f"[connect] discord.ClientException (尝试 {attempt}/3): {e}")
                    # 再次尝试强制获取并断开
                    vc = guild.voice_client
                    if vc is not None:
                        try:
                            await vc.disconnect(force=True)
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)

                except Exception as e:
                    logger.error(f"[connect] 未知错误 (尝试 {attempt}/3): {e}")
                    await asyncio.sleep(2)

            logger.error(f"[connect] 语音频道连接最终失败: {channel.name}")
            return None
        except Exception as e:
            # 所有异常必须在此处被吞掉，不能让它逃逸到框架层
            logger.error(f"[connect_to_voice_channel] 未捕获异常: {e}", exc_info=True)
            return None   # ← 返回 None，绝不 raise

    # ─────────────────────────────────────────────
    #  Keepalive：定期检查连接，断线自动重连
    # ─────────────────────────────────────────────
    async def _keepalive_loop(self, guild_id: int):
        logger.debug(f"[keepalive] guild {guild_id} keepalive 任务启动")
        reconnect_channel = None

        while guild_id in self.voice_clients:
            await asyncio.sleep(20)  # 每 20 秒检查一次

            if guild_id not in self.voice_clients:
                break

            vc = self.voice_clients[guild_id]
            reconnect_channel = vc.channel  # 记录频道用于重连

            if vc.is_connected():
                logger.debug(f"[keepalive] guild {guild_id} 连接正常")
                continue

            # ── 断线重连 ──
            logger.warning(f"[keepalive] guild {guild_id} 检测到连接断开，尝试重连: {reconnect_channel.name}")
            try:
                # 检查是否已经连接到目标频道（可能被其他进程重新连接）
                guild = vc.guild  # 获取 guild 对象
                existing_vc = guild.voice_client
                
                if existing_vc is not None and existing_vc.is_connected():
                    if existing_vc.channel.id == reconnect_channel.id:
                        # 已在目标频道，直接复用
                        logger.info(f"[keepalive] 已在目标频道 {reconnect_channel.name}，复用连接")
                        self.voice_clients[guild_id] = existing_vc
                        continue
                    else:
                        # 已连接但到其他频道，先断开
                        logger.info(f"[keepalive] 从 {existing_vc.channel.name} 移动到 {reconnect_channel.name}")
                        try:
                            await existing_vc.disconnect(force=True)
                        except Exception as e:
                            logger.warning(f"[keepalive] 断开旧连接时出错（忽略）: {e}")
                elif existing_vc is not None:
                    # 对象存在但已断线，强制清理
                    logger.warning(f"[keepalive] 发现已断线的残留 voice_client，强制清理")
                    try:
                        await existing_vc.disconnect(force=True)
                    except Exception:
                        pass

                # 清理旧客户端引用
                self.voice_clients.pop(guild_id, None)
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass

                new_vc = await reconnect_channel.connect(timeout=30.0, reconnect=True)

                # 等待稳定
                for _ in range(10):
                    await asyncio.sleep(0.3)
                    if new_vc.is_connected():
                        break

                if new_vc.is_connected():
                    self.voice_clients[guild_id] = new_vc
                    self.is_playing[guild_id] = False
                    logger.info(f"[keepalive] guild {guild_id} 重连成功: {reconnect_channel.name}")
                    # 重启播放队列（旧的已经退出）
                    asyncio.create_task(self._play_queue_loop(guild_id))
                else:
                    logger.error(f"[keepalive] guild {guild_id} 重连后仍未稳定，放弃")
                    self.voice_clients.pop(guild_id, None)
                    break

            except Exception as e:
                logger.error(f"[keepalive] guild {guild_id} 重连失败: {e}")
                self.voice_clients.pop(guild_id, None)
                break

        logger.debug(f"[keepalive] guild {guild_id} keepalive 任务结束")

    # ─────────────────────────────────────────────
    #  播放队列（含断线重连 + 文件自动清理）
    # ─────────────────────────────────────────────
    async def _play_queue_loop(self, guild_id: int):
        logger.debug(f"[play_queue] guild {guild_id} 播放队列任务启动")

        while guild_id in self.voice_clients:
            # 等待队列中有音频
            try:
                audio_file = await asyncio.wait_for(
                    self.audio_queues[guild_id].get(),
                    timeout=2.0
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"[play_queue] 队列读取失败: {e}")
                break

            if guild_id not in self.voice_clients:
                break

            vc = self.voice_clients[guild_id]

            # ── 若连接断开，等待 keepalive 重连，最多等 15 秒 ──
            if not vc.is_connected():
                logger.warning(f"[play_queue] guild {guild_id} 播放前发现连接断开，等待重连...")
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    if guild_id not in self.voice_clients:
                        break
                    vc = self.voice_clients[guild_id]
                    if vc.is_connected():
                        break
                else:
                    logger.error(f"[play_queue] guild {guild_id} 等待重连超时，丢弃当前音频")
                    self._cleanup_audio_file(audio_file)
                    continue

            if guild_id not in self.voice_clients:
                self._cleanup_audio_file(audio_file)
                break

            # ── 实际播放 ──
            self.is_playing[guild_id] = True
            play_done = asyncio.Event()

            def after_play(error):
                if error:
                    logger.error(f"[play_queue] 播放回调错误: {error}")
                play_done.set()

            try:
                source = discord.FFmpegPCMAudio(
                    audio_file,
                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                )
                vc.play(source, after=after_play)

                # 等待播放结束，同时守护连接状态
                while not play_done.is_set():
                    await asyncio.sleep(0.5)
                    if guild_id in self.voice_clients and not self.voice_clients[guild_id].is_connected():
                        logger.warning(f"[play_queue] 播放中途连接断开，中止当前片段")
                        try:
                            vc.stop()
                        except Exception:
                            pass
                        break

            except Exception as e:
                logger.error(f"[play_queue] 播放异常: {e}")
            finally:
                self.is_playing[guild_id] = False
                self._cleanup_audio_file(audio_file)

        logger.debug(f"[play_queue] guild {guild_id} 播放队列任务结束")

    def _cleanup_audio_file(self, filepath: str):
        """安全删除临时音频文件"""
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            logger.debug(f"[cleanup] 删除临时文件失败: {e}")

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
            guild, user = await self._resolve_guild_and_author(event)
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return

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
            if voice_client is None:
                yield event.plain_result("加入语音频道失败")
            else:
                yield event.plain_result(f"已加入: {voice_channel.name}")
        except Exception as e:
            # 所有异常必须在此处被吞掉，不能让它逃逸到框架层
            logger.error(f"[join_voice_channel] 未捕获异常: {e}", exc_info=True)
            yield event.plain_result(f"加入失败: {e}")
            
    @filter.command("dv_tts_leave")
    async def leave_voice_channel(self, event: AstrMessageEvent):
        """离开语音频道
        
        用法: /dv_tts_leave
        """
        try:
            guild, _ = await self._resolve_guild_and_author(event)
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
            text = event.message_str
            if text.startswith("/dv_tts"):
                text = text[7:].strip()
            if not text:
                yield event.plain_result("请提供要转换的文本")
                return

            guild, user = await self._resolve_guild_and_author(event)
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return

            guild_id = guild.id

            if guild_id not in self.voice_clients:
                member = guild.get_member(user.id)
                if member and member.voice and member.voice.channel:
                    voice_client = await self.connect_to_voice_channel(member.voice.channel)
                    if not voice_client:
                        yield event.plain_result("无法加入语音频道，请先使用 /dv_tts_join 命令")
                        return
                else:
                    yield event.plain_result("请先使用 /dv_tts_join 命令加入语音频道")
                    return

            audio_file = await self.generate_tts_audio(text, guild_id)
            if not audio_file:
                yield event.plain_result("生成语音失败")
                return

            if guild_id in self.audio_queues:
                await self.audio_queues[guild_id].put(audio_file)
                preview = text[:50] + ("..." if len(text) > 50 else "")
                yield event.plain_result(f"已添加到播放队列: {preview}")
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
            guild, _ = await self._resolve_guild_and_author(event)
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return

            guild_id = guild.id
            
            if guild_id not in self.voice_clients:
                yield event.plain_result("当前未连接到任何语音频道")
                return

            await self.interrupt_current_playback(guild_id)
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
            guild, _ = await self._resolve_guild_and_author(event)
            if not guild:
                yield event.plain_result("此命令只能在 Discord 服务器中使用")
                return

            guild_id = guild.id
            status_lines = ["=== TTS 状态 ==="]

            if guild_id in self.voice_clients:
                vc = self.voice_clients[guild_id]
                if vc.is_connected():
                    status_lines.append(f"语音频道: {vc.channel.name}")
                    status_lines.append(f"播放状态: {'播放中' if self.is_playing[guild_id] else '空闲'}")
                else:
                    status_lines.append("语音连接: 已断开")
            else:
                status_lines.append("语音连接: 未连接")

            if guild_id in self.audio_queues:
                status_lines.append(f"队列长度: {self.audio_queues[guild_id].qsize()}")
            else:
                status_lines.append("队列状态: 未初始化")

            status_lines.append(f"中文语音 (edge-tts): {self.tts_voice_zh}")
            status_lines.append(f"日语 VoiceVox Speaker ID: {self.tts_voice_ja_id}")
            status_lines.append(f"edge-tts 语速: {self.tts_rate}")
            status_lines.append(f"edge-tts 音量: {self.tts_volume}")
            status_lines.append(f"VoiceVox 引擎地址: {self.voicevox_url}")
            status_lines.append(f"VoiceVox 语速倍率: {self.tts_voicevox_speed}")
            status_lines.append(f"VoiceVox 音调偏移: {self.tts_voicevox_pitch}")

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
