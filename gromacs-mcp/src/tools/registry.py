"""Tool registry: registers tools with FastMCP and remembers which category each belongs to.

Categories used by list_available_commands: sync, submit, jobs, info, prepare, analysis.
FastMCP's decorator returns the plain function, so registered tools stay directly callable in tests.
"""

from typing import Callable, Dict, List


class ToolRegistry:
    def __init__(self, mcp):
        self.mcp = mcp
        self.categories: Dict[str, List[str]] = {}

    def tool(self, category: str, **kwargs) -> Callable:
        def decorator(fn: Callable) -> Callable:
            names = self.categories.setdefault(category, [])
            if fn.__name__ not in names:
                names.append(fn.__name__)
            return self.mcp.tool(**kwargs)(fn)
        return decorator

    def all_tools(self) -> List[str]:
        return [name for names in self.categories.values() for name in names]
