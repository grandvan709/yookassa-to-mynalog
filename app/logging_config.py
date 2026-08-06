import os
import sys
import logging
from logging.handlers import RotatingFileHandler

ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "gray": "\033[90m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "red_bold": "\033[1;31m",
}

LEVEL_COLORS = {
    logging.DEBUG: ANSI["gray"],
    logging.INFO: ANSI["cyan"],
    logging.WARNING: ANSI["yellow"],
    logging.ERROR: ANSI["red"],
    logging.CRITICAL: ANSI["red_bold"],
}


def use_color() -> bool:
    return os.environ.get("NO_COLOR") not in ("1", "true", "yes")


def colorize(text: str, color: str) -> str:
    if not use_color():
        return text
    return f"{ANSI.get(color, '')}{text}{ANSI['reset']}"


class ColorFormatter(logging.Formatter):
    def __init__(self, use_color: bool = True):
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")
        self.use_color = use_color

    def format(self, record):
        ts = self.formatTime(record, self.datefmt)
        message = record.getMessage()
        level_label = record.levelname[:5].ljust(5)

        if self.use_color:
            color = LEVEL_COLORS.get(record.levelno, "")
            line = (
                f"{ANSI['gray']}{ts}{ANSI['reset']}  "
                f"{color}{level_label}{ANSI['reset']}  "
                f"{message}"
            )
        else:
            line = f"{ts}  {level_label}  {message}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


def setup_logging(log_dir: str):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(ColorFormatter(use_color=use_color()))
    root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        f"{log_dir}/sync.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(ColorFormatter(use_color=False))
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
