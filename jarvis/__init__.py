"""Jarvis：DeepSeek 驱动的 personal agent（包根）。

PROJECT_ROOT 只在这里算一次：项目根目录（python-version/）。
所有需要定位项目内文件或目录的模块都从它派生，模块搬进子包后
路径也不会跟着变。
"""

import os

from jarvis.core.config import __version__

__all__ = ["PROJECT_ROOT", "__version__"]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
