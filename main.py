import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Context, Star, StarTools

# 尝试导入APScheduler，如果失败则使用简单的定时器
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    logger.warning("APScheduler not installed, using simple timer instead")


class DailySummaryPlugin(Star):
    """每日群聊总结插件"""
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 配置项
        self.report_type = config.get("report_type", "daily")
        self.push_time = config.get("push_time", "23:00")
        self.group_ids = config.get("group_ids", [])
        self.max_length = config.get("max_length", 1000)
        self.debug_mode = config.get("debug_mode", False)
        self.summary_prompt = config.get("summary_prompt", "你是一个群聊总结助手，请用轻松幽默的口吻总结群聊内容。")
        
        # 解析昵称映射（格式：QQ号:昵称，每行一个）
        nickname_mapping_str = config.get("nickname_mapping", "")
        self.nickname_mapping = self._parse_nickname_mapping(nickname_mapping_str)
        
        # 解析排除ID（格式：每行一个QQ号）
        exclude_ids_str = config.get("exclude_ids", "")
        self.exclude_ids = self._parse_exclude_ids(exclude_ids_str)
        
        # 处理中英文冒号通用性
        self._normalize_config()
        
        # 调度器
        self.scheduler = None
        self._setup_scheduler()
        
        logger.info(f"DailySummaryPlugin initialized with report_type={self.report_type}, push_time={self.push_time}")
    
    def _parse_nickname_mapping(self, text: str) -> Dict[str, str]:
        """解析昵称映射，格式：QQ号:昵称，每行一个"""
        mapping = {}
        if not text or not text.strip():
            return mapping
        
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            
            # 支持中英文冒号
            line = line.replace("：", ":")
            
            # 分割QQ号和昵称
            parts = line.split(":", 1)
            if len(parts) == 2:
                qq_id = parts[0].strip()
                nickname = parts[1].strip()
                if qq_id and nickname:
                    mapping[qq_id] = nickname
        
        if mapping:
            logger.info(f"Parsed {len(mapping)} nickname mappings")
        
        return mapping
    
    def _parse_exclude_ids(self, text: str) -> List[str]:
        """解析排除ID，格式：每行一个QQ号"""
        exclude_ids = []
        if not text or not text.strip():
            return exclude_ids
        
        for line in text.strip().split("\n"):
            line = line.strip()
            if line:
                exclude_ids.append(line)
        
        if exclude_ids:
            logger.info(f"Parsed {len(exclude_ids)} exclude IDs")
        
        return exclude_ids
    
    def _normalize_config(self):
        """规范化配置项，处理中英文冒号等"""
        # 处理推送时间中的中英文冒号
        if self.push_time:
            # 将中文冒号替换为英文冒号
            self.push_time = self.push_time.replace("：", ":")
            # 验证时间格式
            if not re.match(r"^\d{1,2}:\d{2}$", self.push_time):
                logger.warning(f"Invalid push_time format: {self.push_time}, using default 23:00")
                self.push_time = "23:00"
    
    def _setup_scheduler(self):
        """设置定时调度器"""
        if HAS_APSCHEDULER:
            self.scheduler = AsyncIOScheduler()
            # 解析推送时间
            try:
                hour, minute = self.push_time.split(":")
                trigger = CronTrigger(hour=int(hour), minute=int(minute))
                self.scheduler.add_job(
                    self._push_daily_summary,
                    trigger=trigger,
                    id="daily_summary_push",
                    name="每日群聊总结推送"
                )
                self.scheduler.start()
                logger.info(f"Scheduled daily summary at {self.push_time}")
            except Exception as e:
                logger.error(f"Failed to setup scheduler: {e}")
        else:
            # 使用简单的定时器
            asyncio.create_task(self._simple_timer())
    
    async def _simple_timer(self):
        """简单的定时器实现（当APScheduler不可用时）"""
        while True:
            now = datetime.now()
            target_time = datetime.strptime(self.push_time, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
            
            # 如果目标时间已过，设置为明天
            if now > target_time:
                target_time += timedelta(days=1)
            
            # 计算等待时间
            wait_seconds = (target_time - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            
            # 执行推送
            await self._push_daily_summary()
            
            # 等待一分钟避免重复执行
            await asyncio.sleep(60)
    
    async def _push_daily_summary(self):
        """推送每日总结到配置的群聊"""
        if not self.group_ids:
            logger.warning("No group_ids configured for daily summary push")
            return
        
        for group_id in self.group_ids:
            try:
                summary = await self._generate_summary(group_id)
                if summary:
                    await self._send_to_group(group_id, summary)
                    logger.info(f"Pushed daily summary to group {group_id}")
                else:
                    logger.warning(f"Failed to generate summary for group {group_id}")
            except Exception as e:
                logger.error(f"Error pushing summary to group {group_id}: {e}")
    
    async def _send_to_group(self, group_id: str, message: str):
        """发送消息到指定群聊"""
        try:
            # 构建消息链
            chain = MessageChain().message(message)
            
            # 获取平台适配器实例，拿到真实的 adapter ID
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform:
                logger.error("aiocqhttp platform not found for sending message")
                return
            
            adapter_id = platform.meta().id
            umo = f"{adapter_id}:GroupMessage:{group_id}"
            
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"Failed to send message to group {group_id}: {e}")
            raise
    
    async def _generate_summary(self, group_id: str) -> Optional[str]:
        """生成群聊总结"""
        try:
            # 获取历史消息
            messages = await self._get_group_messages(group_id)
            if not messages:
                return None
            
            # 统计消息
            stats = self._analyze_messages(messages)
            
            # 生成AI总结
            ai_summary = await self._generate_ai_summary(messages)
            
            # 构建报告
            report = self._build_report(stats, ai_summary, group_id)
            
            # 限制字数
            if len(report) > self.max_length:
                report = report[:self.max_length - 3] + "..."
            
            return report
            
        except Exception as e:
            logger.error(f"Error generating summary for group {group_id}: {e}")
            return None
    
    async def _get_group_messages(self, group_id: str) -> List[Dict]:
        """获取群聊历史消息，使用 OneBot 11 的 get_group_msg_history API"""
        try:
            # 获取平台实例
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform:
                logger.error("aiocqhttp platform not found")
                return []
            
            # 获取客户端实例（bot）
            bot = platform.get_client()
            if not bot:
                logger.error("Bot client not found")
                return []
            
            # 计算时间范围
            now = datetime.now()
            if self.report_type == "daily":
                # 今日消息：从今天 00:00 开始
                cutoff_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                # 昨日消息：从昨天 00:00 到今天 00:00
                cutoff_time = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            cutoff_timestamp = cutoff_time.timestamp()
            
            # 分页获取消息
            all_messages = []
            message_seq = 0
            max_rounds = 10
            max_messages = 2000
            
            for round_num in range(max_rounds):
                try:
                    # 调用 OneBot 11 API
                    params = {
                        "group_id": group_id,
                        "count": 200,
                        "message_seq": message_seq,
                        "reverseOrder": True
                    }
                    
                    resp = await bot.api.call_action("get_group_msg_history", **params)
                    
                    if not resp or "messages" not in resp:
                        logger.warning(f"No messages in response for round {round_num}")
                        break
                    
                    messages = resp["messages"]
                    if not messages:
                        break
                    
                    # 处理消息
                    for msg in messages:
                        msg_time = msg.get("time", 0)
                        sender = msg.get("sender", {})
                        sender_id = str(sender.get("user_id", ""))
                        sender_nickname = sender.get("nickname", "")
                        
                        # 提取消息内容
                        content = self._extract_message_content(msg)
                        
                        if content:
                            all_messages.append({
                                "sender_id": sender_id,
                                "sender_nickname": sender_nickname,
                                "content": content,
                                "timestamp": msg_time,
                                "message_seq": msg.get("message_id", 0)
                            })
                    
                    # 更新分页游标
                    if messages:
                        # 获取最旧消息的 seq
                        oldest_msg = min(messages, key=lambda m: m.get("time", float("inf")))
                        new_seq = oldest_msg.get("message_seq", 0)
                        
                        # 检查是否已经获取到足够早的消息
                        oldest_time = oldest_msg.get("time", 0)
                        if oldest_time < cutoff_timestamp:
                            break
                        
                        if new_seq == message_seq:
                            break
                        message_seq = new_seq
                    
                    # 检查消息数量限制
                    if len(all_messages) >= max_messages:
                        break
                        
                except Exception as e:
                    logger.error(f"Error in fetch round {round_num}: {e}")
                    break
            
            if self.debug_mode:
                logger.debug(f"Retrieved {len(all_messages)} raw messages for group {group_id}")
            
            # 根据时间过滤消息
            filtered_messages = self._filter_messages_by_time(all_messages, cutoff_timestamp)
            
            if self.debug_mode:
                logger.debug(f"After filtering: {len(filtered_messages)} messages for group {group_id}")
            
            return filtered_messages
            
        except Exception as e:
            logger.error(f"Error getting messages for group {group_id}: {e}")
            return []
    
    def _extract_message_content(self, msg: Dict) -> str:
        """提取消息内容"""
        # 尝试从 raw_message 获取
        raw_message = msg.get("raw_message", "")
        if raw_message:
            return raw_message
        
        # 尝试从 message 获取
        message_list = msg.get("message", [])
        if isinstance(message_list, list):
            text_parts = []
            for seg in message_list:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        text_parts.append(seg.get("data", {}).get("text", ""))
            return "".join(text_parts)
        
        return ""
    
    def _filter_messages_by_time(self, messages: List[Dict], cutoff_timestamp: float) -> List[Dict]:
        """根据时间过滤消息"""
        now = datetime.now()
        
        if self.report_type == "daily":
            # 今日消息：从今天 00:00 到现在
            end_timestamp = now.timestamp()
        else:
            # 昨日消息：从昨天 00:00 到今天 00:00
            end_timestamp = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        
        filtered = []
        for msg in messages:
            msg_time = msg.get("timestamp", 0)
            if cutoff_timestamp <= msg_time <= end_timestamp:
                filtered.append(msg)
        
        return filtered
    
    def _analyze_messages(self, messages: List[Dict]) -> Dict[str, Any]:
        """分析消息统计"""
        stats = {
            "total_messages": 0,
            "user_messages": {},
            "top_users": [],
            "topics": [],
            "interesting_points": []
        }
        
        # 存储用户昵称
        user_nicknames = {}
        
        for msg in messages:
            sender_id = msg.get("sender_id") or msg.get("user_id")
            
            # 跳过排除ID的消息
            if sender_id and str(sender_id) in self.exclude_ids:
                continue
            
            stats["total_messages"] += 1
            
            # 统计用户消息并记录昵称
            if sender_id:
                if sender_id not in stats["user_messages"]:
                    stats["user_messages"][sender_id] = 0
                    # 记录用户昵称（优先使用消息中的昵称）
                    msg_nickname = msg.get("sender_nickname", "")
                    user_nicknames[sender_id] = self._get_nickname(sender_id, msg_nickname)
                stats["user_messages"][sender_id] += 1
        
        # 获取前三名
        sorted_users = sorted(
            stats["user_messages"].items(),
            key=lambda x: x[1],
            reverse=True
        )[:3]
        
        stats["top_users"] = [
            {"user_id": uid, "count": count, "nickname": user_nicknames.get(uid, str(uid))}
            for uid, count in sorted_users
        ]
        
        return stats
    
    def _get_bot_id(self) -> str:
        """获取bot的ID"""
        # 这里需要根据平台适配器获取bot的ID
        # 暂时返回空字符串，实际实现需要调整
        return ""
    
    def _get_nickname(self, user_id: str, msg_nickname: str = "") -> str:
        """获取用户昵称"""
        # 先从昵称映射中查找
        if user_id in self.nickname_mapping:
            return self.nickname_mapping[user_id]
        
        # 使用消息中的昵称
        if msg_nickname:
            return msg_nickname
        
        # 返回用户ID作为最后手段
        return str(user_id)
    
    async def _generate_ai_summary(self, messages: List[Dict]) -> Dict[str, str]:
        """生成AI总结"""
        try:
            # 准备消息内容用于AI总结（排除指定ID）
            message_texts = []
            for msg in messages:
                sender_id = msg.get("sender_id") or msg.get("user_id")
                
                # 跳过排除ID的消息
                if sender_id and str(sender_id) in self.exclude_ids:
                    continue
                
                content = msg.get("content") or msg.get("message")
                if content:
                    msg_nickname = msg.get("sender_nickname", "")
                    nickname = self._get_nickname(sender_id, msg_nickname) if sender_id else "Unknown"
                    message_texts.append(f"{nickname}: {content}")
            
            if not message_texts:
                return {"topics": "", "interesting_points": "", "overall_summary": ""}
            
            # 构建提示词
            prompt = self._build_summary_prompt(message_texts)
            
            # 调用LLM生成总结
            try:
                # 使用AstrBot的LLM接口
                umo = f"aiocqhttp:group:{messages[0].get('group_id', '')}" if messages else ""
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                
                if provider_id:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                    )
                    
                    # 解析LLM响应
                    response_text = llm_resp.completion_text
                    return self._parse_ai_response(response_text)
                else:
                    logger.warning("No LLM provider available, using fallback summary")
                    return self._generate_fallback_summary(message_texts)
                    
            except Exception as e:
                logger.error(f"Error calling LLM: {e}")
                return self._generate_fallback_summary(message_texts)
            
        except Exception as e:
            logger.error(f"Error generating AI summary: {e}")
            return {"topics": "", "interesting_points": "", "overall_summary": ""}
    
    def _build_summary_prompt(self, messages: List[str]) -> str:
        """构建AI总结的提示词"""
        # 取最后100条消息用于总结（避免token过多）
        recent_messages = messages[-100:] if len(messages) > 100 else messages
        messages_text = "\n".join(recent_messages)
        
        # 使用配置的总结口吻
        style_prompt = self.summary_prompt if self.summary_prompt else "你是一个群聊总结助手，请用轻松幽默的口吻总结群聊内容。"
        
        prompt = f"""{style_prompt}

以下是群聊记录：
{messages_text}

请严格按照以下格式输出，不要添加任何多余内容：

【今日话题】
一句话描述第一个话题
一句话描述第二个话题
一句话描述第三个话题

【今日金句】
xxxx说："xxxxxxx"
xxxx说："xxxxxxx"
xxxx说："xxxxxxx"

【整体总结】
50字以内的总结

注意：
1. 今日话题：直接写话题描述，不要加序号，每行一个话题
2. 今日金句：提取群聊中最有意思、最精辟、反响最好的话，格式为 昵称说："原话"，最多3条
3. 整体总结：50字以内"""
        
        return prompt
    
    def _parse_ai_response(self, response_text: str) -> Dict[str, str]:
        """解析AI响应"""
        result = {"topics": "", "interesting_points": "", "overall_summary": ""}
        
        try:
            # 按【】分割章节
            import re
            sections = re.split(r'【([^】]+)】', response_text)
            
            # sections[0] 是第一个标记前的内容（通常为空）
            # sections[1] 是第一个标记名
            # sections[2] 是第一个标记的内容
            # sections[3] 是第二个标记名
            # sections[4] 是第二个标记的内容
            # ...
            
            for i in range(1, len(sections), 2):
                if i + 1 < len(sections):
                    section_name = sections[i].strip()
                    section_content = sections[i + 1].strip()
                    
                    if "话题" in section_name:
                        result["topics"] = section_content
                    elif "金句" in section_name or "亮点" in section_name or "瞬间" in section_name:
                        result["interesting_points"] = section_content
                    elif "总结" in section_name:
                        result["overall_summary"] = section_content
            
            # 如果没有解析到内容，使用整个响应作为整体总结
            if not any(result.values()):
                result["overall_summary"] = response_text[:200] + "..." if len(response_text) > 200 else response_text
            
            return result
            
        except Exception as e:
            logger.error(f"Error parsing AI response: {e}")
            return {"topics": "", "interesting_points": "", "overall_summary": response_text[:200]}
    
    def _generate_fallback_summary(self, messages: List[str]) -> Dict[str, str]:
        """生成备用总结（当LLM不可用时）"""
        # 简单的消息统计
        total_messages = len(messages)
        
        # 提取常见词汇
        all_text = " ".join(messages)
        common_words = ["技术", "问题", "讨论", "分享", "学习", "项目", "进展", "帮忙", "感谢", "哈哈"]
        found_topics = [word for word in common_words if word in all_text]
        
        topics = "、".join(found_topics[:3]) if found_topics else "日常交流"
        interesting_points = f"共{total_messages}条消息，群友积极参与讨论"
        overall_summary = f"今日群聊活跃，共{total_messages}条消息，氛围良好"
        
        return {
            "topics": topics,
            "interesting_points": interesting_points,
            "overall_summary": overall_summary
        }
    
    def _build_report(self, stats: Dict, ai_summary: Dict, group_id: str) -> str:
        """构建最终报告"""
        now = datetime.now()
        date_str = now.strftime("%Y年%m月%d日")
        
        if self.report_type == "daily":
            title = f"📊 {date_str} 群聊日报"
        else:
            yesterday = now - timedelta(days=1)
            date_str = yesterday.strftime("%Y年%m月%d日")
            title = f"📊 {date_str} 群聊日报"
        
        # 构建报告
        report_lines = [
            title,
            "=" * 30,
            "",
            "📈 消息统计",
            f"• 总消息数：{stats['total_messages']}条",
            ""
        ]
        
        # 添加前三名
        if stats["top_users"]:
            report_lines.append("🏆 活跃度排行榜")
            for i, user in enumerate(stats["top_users"], 1):
                medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
                report_lines.append(f"{medal} {user['nickname']}：{user['count']}条消息")
            report_lines.append("")
        
        # 添加AI总结（直接使用AI返回的格式化内容）
        if ai_summary.get("topics"):
            report_lines.append("💡 今日话题")
            # 给每行添加🟢前缀
            for line in ai_summary["topics"].split("\n"):
                line = line.strip()
                if line and not line.startswith("【"):
                    if not line.startswith("🟢"):
                        line = f"🟢 {line}"
                    report_lines.append(line)
            report_lines.append("")
        
        if ai_summary.get("interesting_points"):
            report_lines.append("💬 今日金句")
            # 金句格式已经由AI生成，直接输出
            for line in ai_summary["interesting_points"].split("\n"):
                line = line.strip()
                if line and not line.startswith("【"):
                    report_lines.append(line)
            report_lines.append("")
        
        if ai_summary.get("overall_summary"):
            report_lines.append("📝 整体总结")
            report_lines.append(ai_summary["overall_summary"])
            report_lines.append("")
        
        # 添加时间戳
        report_lines.extend([
            "-" * 30,
            f"⏰ 生成时间：{now.strftime('%H:%M:%S')}",
            "🤖 由AstrBot每日总结插件生成"
        ])
        
        return "\n".join(report_lines)
    
    @filter.command("总结今日")
    async def cmd_summary_today(self, event: AstrMessageEvent, group_id: str = None):
        """手动触发今日总结。用法：/总结今日 [群号]"""
        logger.info(f"Manual summary today triggered by {event.get_sender_id()}")
        
        # 如果没有指定群号，使用当前群聊
        if not group_id:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("❌ 请指定群号或在群聊中使用此命令")
                return
        
        # 临时修改报告类型为今日
        original_type = self.report_type
        self.report_type = "daily"
        
        try:
            summary = await self._generate_summary(group_id)
            if summary:
                yield event.plain_result(summary)
            else:
                yield event.plain_result("❌ 生成总结失败，请检查群号是否正确")
        except Exception as e:
            logger.error(f"Error in cmd_summary_today: {e}")
            yield event.plain_result(f"❌ 生成总结时发生错误：{str(e)}")
        finally:
            # 恢复原始报告类型
            self.report_type = original_type
    
    @filter.command("总结昨日")
    async def cmd_summary_yesterday(self, event: AstrMessageEvent, group_id: str = None):
        """手动触发昨日总结。用法：/总结昨日 [群号]"""
        logger.info(f"Manual summary yesterday triggered by {event.get_sender_id()}")
        
        # 如果没有指定群号，使用当前群聊
        if not group_id:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("❌ 请指定群号或在群聊中使用此命令")
                return
        
        # 临时修改报告类型为昨日
        original_type = self.report_type
        self.report_type = "yesterday"
        
        try:
            summary = await self._generate_summary(group_id)
            if summary:
                yield event.plain_result(summary)
            else:
                yield event.plain_result("❌ 生成总结失败，请检查群号是否正确")
        except Exception as e:
            logger.error(f"Error in cmd_summary_yesterday: {e}")
            yield event.plain_result(f"❌ 生成总结时发生错误：{str(e)}")
        finally:
            # 恢复原始报告类型
            self.report_type = original_type
    
    @filter.command("summary_help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """📊 每日群聊总结插件 使用说明

🎯 手动命令：
• /总结今日 [群号] - 立即生成今日群聊总结
• /总结昨日 [群号] - 立即生成昨日群聊总结
• /summary_help - 显示此帮助信息

⏰ 自动推送：
• 每天在配置的时间自动推送总结到指定群聊
• 默认推送时间：23:00
• 可在插件配置中修改推送时间和群聊

📝 配置项说明：
• 报告类型：选择总结今日或昨日消息
• 推送时间：设置自动推送的时间（支持中英文冒号）
• 推送群聊：设置需要推送的群号列表
• 昵称名单：设置QQ号到昵称的映射
• 最大字数：限制总结报告的字数

💡 使用示例：
• /总结今日 123456789 - 总结群号123456789的今日消息
• /总结昨日 - 总结当前群聊的昨日消息
• 在配置中设置 group_ids: [123456789, 987654321] 实现自动推送"""
        
        yield event.plain_result(help_text)
    
    async def terminate(self):
        """插件卸载时调用"""
        if self.scheduler:
            self.scheduler.shutdown()
            logger.info("DailySummaryPlugin scheduler shutdown")
        logger.info("DailySummaryPlugin terminated")