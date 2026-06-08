from unreal_agent_player.reporting.quotes import QUOTES, pick_quote


def test_quotes_nonempty_strings():
    assert len(QUOTES) >= 5
    assert all(isinstance(q, str) and q.strip() for q in QUOTES)


def test_pick_quote_returns_member():
    for _ in range(20):
        assert pick_quote() in QUOTES
