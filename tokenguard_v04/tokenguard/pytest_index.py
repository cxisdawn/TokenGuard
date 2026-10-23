"""Bounded inventory of hidden PASSED nodes; never a substitute for test execution."""
from __future__ import annotations

from dataclasses import dataclass
import json

MAX_NODES = 32
MAX_INDEX_CHARS = 2400


@dataclass
class PassedNode:
    first_line: int
    last_line: int
    count: int = 1


class PassedIndex:
    """Keep every hidden base node or reject the entire indexed compression.

    Parameter IDs belong to the original artifact. Line spans are envelopes,
    not a claim that all intervening lines are PASSED records.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, PassedNode] = {}
        self.overflow = False
        self._name_chars = 0

    def add(self, node: str, line: int) -> None:
        if self.overflow:
            return
        # Only strip a parameter suffix from the final node component, never
        # from a filename/class containing '['.
        parent, _, leaf = node.rpartition('::')
        name = parent + '::' + leaf.partition('[')[0]
        if name in self.nodes:
            row = self.nodes[name]
            row.last_line = line
            row.count += 1
            return
        # Account for escaped control characters before retaining a new name.
        length = len(json.dumps(name, ensure_ascii=False))
        if len(self.nodes) >= MAX_NODES or self._name_chars + length > MAX_INDEX_CHARS:
            self.overflow = True
            return
        self._name_chars += length
        self.nodes[name] = PassedNode(line, line)

    def render(self) -> str | None:
        if self.overflow:
            return None
        if not self.nodes:
            return ''
        lines = ['[TokenGuard pytest PASSED index; parameter IDs grouped; raw artifact line spans]\n']
        for name, row in self.nodes.items():
            lines.append(f'{json.dumps(name, ensure_ascii=False)}: {row.count} passed; '
                         f'raw lines {row.first_line}-{row.last_line}\n')
        lines.append('[TokenGuard: spans may include other output; recover parameter IDs with grep/show.]\n')
        result = ''.join(lines)
        return result if len(result) <= MAX_INDEX_CHARS else None
