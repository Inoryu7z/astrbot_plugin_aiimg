import time


class Debouncer:
    """基于时间窗口的防抖器（支持 TTL 自动清理）"""

    def __init__(self, config: dict):
        """
        Args:
            interval: 防抖时间（秒）
            ttl: 操作记录最长保留时间（秒）
            cleanup_threshold: 记录数量超过该值时触发清理
        """
        self._interval = config.get("debounce_interval", 3)
        self._ttl = 300
        self._cleanup_threshold = 100

        self._records: dict[str, float] = {}

        # LLM 工具去重缓存（防止 ToolLoop 重复调用）
        self._llm_dedup_cache: dict[str, float] = {}
        self._llm_dedup_ttl = config.get("llm_dedup_ttl", 120)  # 默认 120 秒

        # LLM 工具单条消息受理计数（限制一条用户消息内同一工具的受理总次数）
        self._llm_gen_count: dict[str, tuple[int, float]] = {}
        self._llm_gen_ttl = 300

    def hit(self, key: str) -> bool:
        """
        记录一次操作并判断是否命中防抖

        Returns:
            True  -> 需要拒绝（命中防抖）
            False -> 允许通过
        """
        now = time.time()

        if len(self._records) >= self._cleanup_threshold:
            self._cleanup(now)

        last = self._records.get(key)
        if last is not None and now - last < self._interval:
            return True

        self._records[key] = now
        return False

    def _cleanup(self, now: float) -> None:
        """清理过期记录"""
        expired = [k for k, ts in self._records.items() if now - ts > self._ttl]
        for k in expired:
            self._records.pop(k, None)

    def clear_all(self) -> None:
        """清空所有记录"""
        self._records.clear()
        self._llm_dedup_cache.clear()
        self._llm_gen_count.clear()

    # ==================== LLM 工具去重 ====================

    def llm_tool_is_duplicate(
        self, message_id: str, origin: str, fingerprint: str = ""
    ) -> bool:
        """检查 LLM 工具调用是否重复（用于阻止 ToolLoop 重复调用）

        Args:
            message_id: 消息 ID
            origin: 统一来源标识 (unified_msg_origin)
            fingerprint: 调用指纹（工具名+归一化参数哈希）。同消息同指纹才算重复，
                不同参数的合法连续调用（如一次生成多张不同图）不受影响。

        Returns:
            True  -> 重复调用，应拒绝
            False -> 首次调用，允许并记录
        """
        now = time.time()
        key = f"{origin}:{message_id}:{fingerprint}"

        # 清理过期缓存
        if len(self._llm_dedup_cache) >= self._cleanup_threshold:
            self._cleanup_llm_dedup(now)

        # 检查是否存在且未过期
        ts = self._llm_dedup_cache.get(key)
        if ts is not None and now - ts < self._llm_dedup_ttl:
            return True  # 重复

        # 记录本次调用
        self._llm_dedup_cache[key] = now
        return False

    def llm_tool_clear_duplicate(
        self, message_id: str, origin: str, fingerprint: str = ""
    ) -> None:
        """清除去重记录（任务失败后允许 LLM 以相同参数立即重试）"""
        key = f"{origin}:{message_id}:{fingerprint}"
        self._llm_dedup_cache.pop(key, None)

    def llm_tool_bump_generation(
        self, message_id: str, origin: str, limit: int
    ) -> bool:
        """受理计数：一条用户消息内同一工具的受理次数上限。

        Args:
            message_id: 消息 ID
            origin: 统一来源标识
            limit: 上限次数

        Returns:
            True  -> 放行（已登记本次）
            False -> 超过上限，应拒绝（不登记）
        """
        now = time.time()
        key = f"{origin}:{message_id}"
        if len(self._llm_gen_count) >= self._cleanup_threshold:
            self._cleanup_llm_gen(now)
        count = int(self._llm_gen_count.get(key, (0, 0.0))[0])
        if count >= limit:
            return False
        self._llm_gen_count[key] = (count + 1, now)
        return True

    def _cleanup_llm_gen(self, now: float) -> None:
        """清理过期的受理计数（值为 (计数, 时间戳) 二元组，按时间戳判过期）"""
        expired = [
            k for k, (_, ts) in self._llm_gen_count.items()
            if now - ts > self._llm_gen_ttl
        ]
        for k in expired:
            self._llm_gen_count.pop(k, None)

    def _cleanup_llm_dedup(self, now: float) -> None:
        """清理过期的 LLM 去重缓存"""
        expired = [k for k, ts in self._llm_dedup_cache.items() if now - ts > self._llm_dedup_ttl]
        for k in expired:
            self._llm_dedup_cache.pop(k, None)
