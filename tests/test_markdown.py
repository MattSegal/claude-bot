from claude_bot.markdown import split_markdown


def test_short_text_is_one_piece() -> None:
    assert split_markdown("hello\n\nworld") == ["hello\n\nworld"]
    assert split_markdown("   ") == []


def test_splits_at_line_boundaries_without_losing_text() -> None:
    paragraphs = [f"paragraph {index} " + "x" * 40 for index in range(10)]
    text = "\n\n".join(paragraphs)

    pieces = split_markdown(text, limit=120)

    assert len(pieces) > 1
    assert all(len(piece) <= 120 for piece in pieces)
    assert "\n".join(pieces) == text


def test_reopens_code_fence_across_pieces() -> None:
    code_lines = "\n".join(f"line {index}" for index in range(30))
    text = f"Intro\n\n```\n{code_lines}\n\nmore\n```\n\nOutro"

    pieces = split_markdown(text, limit=100)

    assert len(pieces) >= 2
    for piece in pieces:
        assert len(piece) <= 100
        assert piece.count("```") % 2 == 0, f"unbalanced fence in piece: {piece!r}"
    assert pieces[0].startswith("Intro")
    assert pieces[1].startswith("```\n")
    assert pieces[-1].endswith("Outro")


def test_hard_wraps_a_single_oversized_line() -> None:
    text = "x" * 500

    pieces = split_markdown(text, limit=120)

    assert all(len(piece) <= 120 for piece in pieces)
    assert "".join(pieces) == text
