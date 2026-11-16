import os
from datetime import datetime

# ================== 使用示例 =====================
# from log_recording import Logger
# log = Logger().log
# log("test")
# =================================================

class Logger:
    """日志记录器（单例模式）"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def log(self, message: str) -> None:
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)

        now = datetime.now()
        # 根据月分割文件
        log_file_name = f"{now.year}-{now.month:02d}.log"
        log_file_path = os.path.join(log_dir, log_file_name)

        # 构造日志格式
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"

        # 追加写入日志文件
        try:
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(log_line)
        except Exception as e:
            print(f"写日志失败: {e}")


