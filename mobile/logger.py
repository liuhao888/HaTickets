# -*- coding: UTF-8 -*-
"""
Unified logging framework for HaTickets mobile module.

Usage:
    from logger import get_logger, log_event, start_run_session, finish_run_session
    logger = get_logger(__name__)
    start_run_session()
    logger.info("消息内容")
    log_event(logger, "sale_ready", cta_text="立即购票", polls=12, duration_ms=480)
    log_strategy(logger, action="select_price", strategy="index_coords", price_index=0)
    finish_run_session(outcome="success")
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Set

# Asia/Shanghai timezone (UTC+8)
_TZ_SHANGHAI = timezone(timedelta(hours=8))

# 项目根目录 /log/YYYYMMDD_HHMMSS.log
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_RUN_LOG_DIR = _REPO_ROOT / "log"

_LEGACY_LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hatickets_mobile.log"
)

_CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_RUN_LOG_NAME_FORMAT = "%Y%m%d_%H%M%S"

_ANSI_RESET = "\033[0m"
_ANSI_COLORS = {
    logging.DEBUG: "\033[2m",
    logging.INFO: "\033[36m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}


class _ShanghaiFormatter(logging.Formatter):
    """Logging formatter that uses Asia/Shanghai timezone for timestamps."""

    def formatTime(
        self, record: logging.LogRecord, datefmt: Optional[str] = None
    ) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_TZ_SHANGHAI)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime(_DATE_FORMAT)


class _ShanghaiColorFormatter(_ShanghaiFormatter):
    """Console formatter with optional ANSI colors based on log level."""

    def __init__(self, *args, enable_color: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.enable_color = enable_color

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if not self.enable_color:
            return rendered

        color = _ANSI_COLORS.get(record.levelno)
        if not color:
            return rendered
        return f"{color}{rendered}{_ANSI_RESET}"


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE") == "1":
        return True
    if stream is None or not hasattr(stream, "isatty"):
        return False
    if not stream.isatty():
        return False
    return os.environ.get("TERM", "").lower() != "dumb"


def _build_console_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        _ShanghaiColorFormatter(
            fmt=_CONSOLE_FORMAT,
            datefmt=_DATE_FORMAT,
            enable_color=_supports_color(getattr(handler, "stream", None)),
        )
    )
    return handler


def _build_file_handler(path: str) -> logging.FileHandler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_ShanghaiFormatter(fmt=_FILE_FORMAT, datefmt=_DATE_FORMAT))
    return handler


# Module-level flag to avoid adding duplicate handlers on repeated get_logger() calls.
_configured_loggers: Set[str] = set()

# 单次执行会话落盘（log/YYYYMMDD_HHMMSS.log）
_session_handler: Optional[logging.FileHandler] = None
_session_path: Optional[Path] = None
_session_started_mono: Optional[float] = None
_session_started_at: Optional[str] = None


def get_run_log_path() -> Optional[Path]:
    """当前执行会话日志路径；未开启会话时返回 None。"""
    return _session_path


def _attach_session_handler(logger: logging.Logger) -> None:
    if _session_handler is None:
        return
    if _session_handler in logger.handlers:
        return
    logger.addHandler(_session_handler)


def start_run_session(
    *,
    log_dir: Optional[Path] = None,
    stamp: Optional[str] = None,
) -> Path:
    """开启一次执行会话日志：``log/YYYYMMDD_HHMMSS.log``。

    重复调用会先结束上一次会话。返回新建日志文件路径。
    """
    global _session_handler, _session_path, _session_started_mono, _session_started_at

    if _session_handler is not None:
        finish_run_session(outcome="superseded")

    if log_dir is not None:
        target_dir = Path(log_dir)
    else:
        env_dir = os.environ.get("HATICKETS_LOG_DIR", "").strip()
        target_dir = Path(env_dir) if env_dir else _DEFAULT_RUN_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=_TZ_SHANGHAI)
    name = stamp or now.strftime(_RUN_LOG_NAME_FORMAT)
    path = target_dir / f"{name}.log"

    handler = _build_file_handler(str(path))
    _session_handler = handler
    _session_path = path
    _session_started_mono = time.monotonic()
    _session_started_at = now.isoformat()

    for logger_name in list(_configured_loggers):
        _attach_session_handler(logging.getLogger(logger_name))

    boot = logging.getLogger("mobile.run_session")
    if "mobile.run_session" not in _configured_loggers:
        # 轻量会话 logger：只挂会话文件，避免重复 console/legacy 文件。
        boot.setLevel(logging.DEBUG)
        boot.propagate = False
        _configured_loggers.add("mobile.run_session")
    _attach_session_handler(boot)
    boot.info(
        "======== run session start path=%s started_at=%s ========",
        path,
        _session_started_at,
    )
    return path


def finish_run_session(
    *,
    outcome: Optional[str] = None,
    exit_code: Optional[int] = None,
    terminal_reason: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Optional[Path]:
    """结束会话：写入耗时摘要并关闭文件句柄。"""
    global _session_handler, _session_path, _session_started_mono, _session_started_at

    path = _session_path
    handler = _session_handler
    if handler is None:
        return None

    duration_ms = None
    if _session_started_mono is not None:
        duration_ms = int((time.monotonic() - _session_started_mono) * 1000)

    boot = logging.getLogger("mobile.run_session")
    parts = [
        "======== run session end",
        f"path={path}",
        f"started_at={_session_started_at or 'none'}",
        f"finished_at={datetime.now(tz=_TZ_SHANGHAI).isoformat()}",
    ]
    if duration_ms is not None:
        parts.append(f"duration_ms={duration_ms}")
    if outcome is not None:
        parts.append(f"outcome={outcome}")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    if terminal_reason:
        parts.append(f"terminal_reason={terminal_reason}")
    if extra:
        for key, value in extra.items():
            parts.append(f"{key}={_format_event_value(value)}")
    parts.append("========")
    try:
        boot.info(" ".join(parts))
    except Exception:
        pass

    for logger_name in list(_configured_loggers):
        logger = logging.getLogger(logger_name)
        if handler in logger.handlers:
            logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass

    _session_handler = None
    _session_path = None
    _session_started_mono = None
    _session_started_at = None
    return path


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger that writes to console (INFO+) and file (DEBUG+).

    Args:
        name: Logger name, typically ``__name__`` of the calling module.

    Returns:
        A :class:`logging.Logger` instance with Shanghai-timezone timestamps.
    """
    logger = logging.getLogger(name)

    if name not in _configured_loggers:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(_build_console_handler())
        logger.addHandler(_build_file_handler(_LEGACY_LOG_FILE))
        # Prevent propagation to the root logger to avoid duplicate output.
        logger.propagate = False
        _configured_loggers.add(name)

    _attach_session_handler(logger)
    return logger


def _format_event_value(value) -> str:
    """Render a field value for structured event logs (no whitespace, quote-safe)."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # Collapse whitespace so a single line stays parseable as `key=value` pairs.
    text = " ".join(text.split())
    if not text:
        return '""'
    if any(c in text for c in (" ", "\t", "=", '"')):
        text = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{text}"'
    return text


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields,
) -> None:
    """Emit a structured event log line.

    Output shape: ``event=<name> key1=value1 key2=value2``.  Values containing
    whitespace, ``=`` or quotes are quoted/escaped so the line stays grep-able
    and downstream-parseable.

    Args:
        logger: Target logger (typically obtained via :func:`get_logger`).
        event: Short snake_case event name (e.g. ``sale_ready``).
        level: Logging level (default :data:`logging.INFO`).
        **fields: Arbitrary key/value pairs serialized to the log line.
    """
    parts = [f"event={_format_event_value(event)}"]
    for key, value in fields.items():
        parts.append(f"{key}={_format_event_value(value)}")
    logger.log(level, " ".join(parts))


def log_strategy(
    logger: logging.Logger,
    *,
    action: str,
    strategy: str,
    level: int = logging.INFO,
    **fields,
) -> None:
    """记录点击/匹配策略，便于事后对比「直接坐标点击」与「文本匹配」等路径。

    例::
        log_strategy(logger, action="select_price", strategy="index_coords", price_index=0)
        log_strategy(logger, action="select_price", strategy="text_match", price="588")
        log_strategy(logger, action="submit_order", strategy="labeled_button_coords")
    """
    log_event(
        logger,
        "click_strategy",
        level=level,
        action=action,
        strategy=strategy,
        **fields,
    )


def log_step(
    logger: logging.Logger,
    step: str,
    *,
    duration_ms: int,
    level: int = logging.INFO,
    **fields,
) -> None:
    """记录主流程单步耗时。

    同时打结构化事件与可读中文行，方便 grep ``event=step_timing``。
    """
    log_event(
        logger,
        "step_timing",
        level=level,
        step=step,
        duration_ms=int(duration_ms),
        **fields,
    )
    logger.log(
        level,
        "步骤耗时[%s] %.2fs%s",
        step,
        duration_ms / 1000.0,
        "".join(f" {key}={_format_event_value(value)}" for key, value in fields.items()),
    )


def log_wait(
    logger: logging.Logger,
    *,
    kind: str,
    waited_ms: int,
    planned_ms: Optional[int] = None,
    level: int = logging.INFO,
    **fields,
) -> None:
    """记录等待时长（开售倒计时、轮询、sleep 等）。"""
    payload = {
        "kind": kind,
        "waited_ms": int(waited_ms),
    }
    if planned_ms is not None:
        payload["planned_ms"] = int(planned_ms)
    payload.update(fields)
    log_event(logger, "wait", level=level, **payload)
    planned_part = (
        f" planned={planned_ms}ms" if planned_ms is not None else ""
    )
    extra = "".join(
        f" {key}={_format_event_value(value)}"
        for key, value in fields.items()
    )
    logger.log(
        level,
        "等待耗时[%s] waited=%dms%s%s",
        kind,
        int(waited_ms),
        planned_part,
        extra,
    )
