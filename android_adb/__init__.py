"""Android ADB 鼠标侧键控制器。

分层结构（便于单独替换/扩展）：
    config          配置加载（config.json / 环境变量 / 默认值）
    logger          控制台日志初始化
    adb_client      ADB 可执行文件查找、设备发现、命令执行
    media_state     dumpsys media_session 解析（播放状态、位置、媒体按钮会话）
    play_pause      播放/暂停：媒体键 / 点击画面按钮 / 自动兜底
    rewind          后退：横滑手势（默认，闭环校准）/ 媒体键
    backends/       按键后端：ADB（默认）/ Scrcpy（预留）/ DryRun（演练）
    controller      动作编排：鼠标动作 -> 后端调用
    mouse_listener  全局鼠标监听、防抖、异步指令队列、钩子看门狗
    hotkeys         退出快捷键（默认 Ctrl+Alt+Q，刻意不用 Esc）
"""

__version__ = "1.0.0"
