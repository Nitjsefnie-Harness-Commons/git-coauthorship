"""Co-author trailers as git parses them."""
import re
import subprocess

COAUTHOR = re.compile(r"^co-authored-by[ \t]*:[ \t]*(.*\S|)", re.I | re.M | re.A)
_KEY = re.compile(r"[A-Za-z0-9-]+[ \t]*:", re.A)


def messages(*revs):
    """hash -> (message, its %(trailers) block)."""
    out = subprocess.check_output(
        ["git", "log", "-z", "--format=%H%x00%B%x00%(trailers)", *revs],
        timeout=300)
    tokens = out.decode("utf-8", "surrogateescape").split("\0")
    return {tokens[i]: (tokens[i + 1], tokens[i + 2])
            for i in range(0, len(tokens) - 2, 3)}


def with_trailer(message, block, trailer, replace=False):
    """Join `trailer` to `block`, else open a paragraph."""
    if not block:
        return message.rstrip("\n") + f"\n\n{trailer}\n"
    start = message.rfind("\n" + block) + 1
    if not start:
        raise ValueError(f"trailer block not in the message: {block!r}")
    lines = block.rstrip("\n").split("\n")
    kept, dropping = [], False
    for line in lines:
        folded = line[:1] in (" ", "\t")
        dropping = replace and (COAUTHOR.match(line) or dropping and folded)
        if not dropping:
            kept.append(line)
    kept.append(trailer)
    if len(kept) <= len(lines):
        kept = _parsed_layout(kept)
    return (message[:start] + "".join(line + "\n" for line in kept)
            + message[start + len(block):])


def _parsed_layout(lines):
    """git parses a block only while trailers * 3 >= other lines."""
    trailer, kinds = False, []
    for line in lines:
        if line[:1] not in (" ", "\t"):
            trailer = bool(_KEY.match(line))
        kinds.append(trailer)
    others = [line for line, kind in zip(lines, kinds) if not kind]
    count = sum(kind and line[:1] not in (" ", "\t")
                for line, kind in zip(lines, kinds))
    if count * 3 >= len(others):
        return lines
    return others + [""] + [line for line, kind in zip(lines, kinds) if kind]
