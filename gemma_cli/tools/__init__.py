"""Tool definitions and executor for the local agent."""

from .definitions import TOOLS, OLLAMA_TOOLS
from .executor import execute_tool

__all__ = ["TOOLS", "OLLAMA_TOOLS", "execute_tool"]
