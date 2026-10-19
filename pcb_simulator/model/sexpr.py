"""Tokenizer for KiCad s-expression files.

Unknown lists stay in the tree so a newer KiCad token does not abort a parse.
The caller skips heads it does not recognize.
"""

from __future__ import annotations


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(text)
    if text.startswith("\ufeff"):
        i = 1
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c in "()":
            tokens.append(c)
            i += 1
            continue
        if c == '"':
            i += 1
            buf: list[str] = []
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            tokens.append("".join(buf))
            continue
        j = i + 1
        while j < n and not text[j].isspace() and text[j] not in "()":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def _atom(token: str):
    if token == "":
        return token
    try:
        if any(ch in token for ch in ".eE"):
            return float(token)
        return int(token)
    except ValueError:
        return token


def parse_sexpr(text: str):
    """Parse one top-level s-expression into nested lists of str/int/float."""
    tokens = tokenize(text)
    if not tokens or tokens[0] != "(":
        raise ValueError("File does not start with an s-expression")

    def read(pos: int):
        if pos >= len(tokens) or tokens[pos] != "(":
            raise ValueError("Expected '('")
        pos += 1
        node = []
        while pos < len(tokens) and tokens[pos] != ")":
            if tokens[pos] == "(":
                child, pos = read(pos)
                node.append(child)
            else:
                node.append(_atom(tokens[pos]))
                pos += 1
        if pos >= len(tokens) or tokens[pos] != ")":
            raise ValueError("Unclosed '('")
        return node, pos + 1

    tree, pos = read(0)
    return tree
