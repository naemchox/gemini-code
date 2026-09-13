"""Terminal line input shared by both modes."""


def read_prompt() -> str:
    """Read one message. A line ending with a backslash continues on the next line."""
    lines = []
    prompt = "\001\033[1;32m\002> \001\033[0m\002"
    while True:
        line = input(prompt)
        if line.endswith("\\"):
            lines.append(line[:-1])
            prompt = "\001\033[2m\002. \001\033[0m\002"
            continue
        lines.append(line)
        return "\n".join(lines).strip()


def enable_line_editing() -> None:
    try:
        import readline  # noqa: F401  (arrow keys and input history)
    except ImportError:
        pass
