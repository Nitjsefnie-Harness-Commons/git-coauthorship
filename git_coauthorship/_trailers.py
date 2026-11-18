"""Co-author trailers judged and written the way git parses them."""
import re
import subprocess

# A trailer line keyed `co-authored-by` in any case; group 1 is its trimmed value.
COAUTHOR = re.compile(r"^co-authored-by[ \t]*:[ \t]*(.*\S|)", re.I | re.M)


def messages(*revs):
    """hash -> (message, its trailer block as git parses it) over `revs`."""
    out = subprocess.check_output(
        ["git", "log", "-z", "--format=%H%x00%B%x00%(trailers)", *revs],
        timeout=300)
    tokens = out.decode("utf-8", "surrogateescape").split("\0")
    return {tokens[i]: (tokens[i + 1], tokens[i + 2])
            for i in range(0, len(tokens) - 2, 3)}


def with_trailer(message, block, trailer, replace=False):
    """`message` with `trailer` ending `block`, else as a new paragraph;
    `replace` first drops the block's co-author trailers."""
    if not block:
        return message.rstrip("\n") + f"\n\n{trailer}\n"
    start = message.rfind("\n" + block) + 1
    if not start:
        raise ValueError(f"trailer block not in the message: {block!r}")
    kept, dropping = [], False
    for line in block.rstrip("\n").split("\n"):
        # A line opening with whitespace continues the trailer above it.
        dropping = replace and (COAUTHOR.match(line) or dropping
                                and line[:1] in (" ", "\t"))
        if not dropping:
            kept.append(line + "\n")
    return (message[:start] + "".join(kept) + f"{trailer}\n"
            + message[start + len(block):])
