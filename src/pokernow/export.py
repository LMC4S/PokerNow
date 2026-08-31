"""Write derived, human/LLM-readable files next to a game's raw data.

After every refresh the game folder contains:

    poker-now-hands-game-<id>.json   raw hands (PokerNow format)
    poker_now_log_<id>.csv           raw session log (PokerNow format)
    ledger-<id>.json                 raw ledger
    meta.json                        fetch bookkeeping
    stats.json                       session + per-player stats (machine readable)
    players.csv                      one row per player (same numbers as stats.json)
    hands.csv                        one row per hand: time, pot, board, winners, per-player net
    summary.md                       a readable digest: table, leaderboard, notable hands

Everything is regenerated from the raw files, so deleting them is always safe.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime
from typing import Any

from .models import Session
from .stats import SessionStats, compute_session_stats

PLAYER_COLUMNS = [
    "name", "player", "aka", "hands", "net", "net_bb", "bb_per_100", "vpip", "pfr", "three_bet", "af",
    "wtsd_pct", "wsd_pct", "cbet_pct", "fold_to_cbet_pct", "win_pct", "hands_won", "all_ins",
    "biggest_pot_won", "total_won", "total_invested", "bets_raises", "calls", "folds", "checks",
    "first_stack", "last_stack",
]


def _short(key: str) -> str:
    return key.rsplit(" @ ", 1)[0]


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return "∞" if v >= 999 else f"{v:.1f}"
    return str(v)


def _atomic_write(path: str, data: str) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(data)
    os.replace(tmp, path)


def players_csv(stats: SessionStats) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(PLAYER_COLUMNS)
    for p in stats.players:
        d = p.to_dict(stats.big_blind)
        d["aka"] = "; ".join(p.aka)
        w.writerow([d.get(c) for c in PLAYER_COLUMNS])
    return buf.getvalue()


def hands_csv(session: Session, stats: SessionStats) -> str:
    players = [p.player for p in stats.players]
    names = [p.name for p in stats.players]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["hand", "id", "started_at", "dealer", "players", "pot", "pot_bb", "board", "board_2", "winners",
                "showdown", "bomb_pot", "double_board", "run_it_twice", "hero_cards", "shown_cards"] + [f"net:{n}" for n in names])
    bb = stats.big_blind
    for h in session.hands:
        w.writerow([
            h.number, h.id, h.started_at.isoformat() if h.started_at else "", _short(h.dealer or ""),
            len(h.players), h.pot, round(h.pot / bb, 1) if bb else "", " ".join(h.board),
            " ".join(h.board_runs[1]) if len(h.board_runs) > 1 else "",
            "; ".join(_short(p) for p in h.winners), int(h.went_to_showdown), int(h.bomb_pot), int(h.double_board),
            int(h.run_it_twice), " ".join(h.hero_cards),
            "; ".join(f"{_short(p)}: {' '.join(c)}" for p, c in h.known_cards.items()),
        ] + [h.net(p) if p in h.players else "" for p in players])
    return buf.getvalue()


def summary_md(session: Session, stats: SessionStats, game_id: str | None = None) -> str:
    bb = stats.big_blind
    lines: list[str] = []
    title = f"PokerNow session {game_id}" if game_id else "PokerNow session"
    lines.append(f"# {title}")
    lines.append("")
    started = stats.started_at[:16].replace("T", " ") if stats.started_at else "?"
    lines.append(f"- **Game:** {', '.join(stats.game_types) or '?'} · blinds {stats.small_blind}/{stats.big_blind}")
    pace = f" · ≈{stats.hands_per_hour} hands/hour" if stats.hands_per_hour else ""
    lines.append(f"- **Hands:** {stats.hands} · started {started} UTC · duration {stats.duration_minutes} min{pace}")
    lines.append(f"- **Preflop:** raised pots {stats.raised_pot_pct}% · 3-bet+ {stats.threebet_pot_pct}% · limped {stats.limp_pot_pct}%")
    lines.append(f"- **Pots:** avg {stats.avg_pot} ({round(stats.avg_pot / bb, 1) if bb and stats.avg_pot else '?'} bb), biggest {stats.biggest_pot} (hand #{stats.biggest_pot_hand})")
    lines.append(f"- **Saw flop:** {stats.flop_pct}% of hands · **showdown:** {stats.showdown_pct}% · avg {stats.avg_players_per_hand} players/hand")
    if session.hero:
        lines.append(f"- **Hero (log owner):** {_short(session.hero)}")
    if stats.unparsed_lines:
        lines.append(f"- ⚠️ {stats.unparsed_lines} log line(s) not understood by the parser")
    lines.append("")
    lines.append("## Players")
    lines.append("")
    lines.append("| Player | Hands | Net | Net (bb) | bb/100 | VPIP | PFR | 3-Bet | AF | WTSD | W$SD | C-Bet | Fold→CB | All-ins | Biggest pot |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in stats.players:
        d = p.to_dict(bb)
        label = p.name + (f" (aka {', '.join(p.aka)})" if p.aka else "")
        lines.append(
            f"| {label} | {p.hands} | {p.net:+d} | {_fmt(d['net_bb'])} | {_fmt(d['bb_per_100'])} | {_fmt(p.vpip)} | {_fmt(p.pfr)} | "
            f"{_fmt(p.three_bet)} | {_fmt(d['af'])} | {_fmt(p.wtsd_pct)} | {_fmt(p.wsd_pct)} | {_fmt(p.cbet_pct)} | "
            f"{_fmt(p.fold_to_cbet_pct)} | {p.all_ins} | {p.biggest_pot_won} |"
        )
    lines.append("")
    lines.append("Stat definitions: VPIP/PFR/3-Bet are preflop percentages; AF = (bets+raises)/calls; WTSD = went to showdown / saw flop; "
                 "W$SD = won at showdown / went to showdown; C-Bet = flop continuation bet by the preflop aggressor; net excludes uncalled bets.")
    lines.append("")

    # Events (joins/quits/rebuys)
    if session.events:
        lines.append("## Table events")
        lines.append("")
        shown = 0
        for e in session.events:
            if e.kind in ("info", "config", "requested_seat"):
                continue
            t = e.at.strftime("%H:%M") if e.at else "--:--"
            who = _short(e.player) if e.player else ""
            amt = f" {e.amount}" if e.amount is not None else ""
            lines.append(f"- {t} {e.kind.replace('_', ' ')} {who}{amt}")
            shown += 1
            if shown >= 60:
                lines.append(f"- … {len(session.events) - shown} more")
                break
        lines.append("")

    # Notable hands: top 10 by pot
    big = sorted(session.hands, key=lambda h: -h.pot)[:10]
    lines.append("## Biggest pots")
    lines.append("")
    for h in big:
        winners = ", ".join(_short(p) for p in h.winners)
        board = " ".join(h.board) if h.board else "no flop"
        nets = ", ".join(f"{_short(p)} {h.net(p):+d}" for p in h.players if h.net(p) != 0)
        cards = "; ".join(f"{_short(p)} {' '.join(c)}" for p, c in h.known_cards.items())
        lines.append(f"- **#{h.number}** pot {h.pot}{f' ({h.pot / bb:.0f} bb)' if bb else ''} · board {board} · won by {winners} · {nets}" + (f" · cards: {cards}" if cards else ""))
    lines.append("")

    # Per-player swing
    lines.append("## Per-player session arc")
    lines.append("")
    for p in stats.players:
        acc = 0
        peak = trough = 0
        for v in p.net_by_hand:
            acc += v
            peak = max(peak, acc)
            trough = min(trough, acc)
        lines.append(f"- {p.name}{(' (aka ' + ', '.join(p.aka) + ')') if p.aka else ''}: finished {p.net:+d}, peak {peak:+d}, trough {trough:+d}, won {p.hands_won}/{p.hands} hands")
    lines.append("")
    lines.append("## Files")
    lines.append("")
    lines.append("- `hands.csv` — one row per hand (pot, board, winners, per-player net, revealed cards)")
    lines.append("- `players.csv` / `stats.json` — the numbers above, machine readable")
    lines.append("- `poker-now-hands-game-*.json` — every action of every hand (PokerNow's native format)")
    lines.append("- `poker_now_log_*.csv` — the session log (table events, rebuys, admin actions)")
    lines.append("")
    lines.append(f"_Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC by pokernow-hand-history._")
    return "\n".join(lines) + "\n"


def write_exports(game_dir: str, session: Session, stats: SessionStats | None = None, game_id: str | None = None) -> dict[str, str]:
    stats = stats or compute_session_stats(session)
    os.makedirs(game_dir, exist_ok=True)
    out = {
        "stats": os.path.join(game_dir, "stats.json"),
        "players": os.path.join(game_dir, "players.csv"),
        "hands": os.path.join(game_dir, "hands.csv"),
        "summary": os.path.join(game_dir, "summary.md"),
    }
    payload = stats.to_dict()
    payload["game_id"] = game_id
    payload["hero"] = session.hero
    payload["source_format"] = session.source_format
    _atomic_write(out["stats"], json.dumps(payload, ensure_ascii=False, indent=1))
    _atomic_write(out["players"], players_csv(stats))
    _atomic_write(out["hands"], hands_csv(session, stats))
    _atomic_write(out["summary"], summary_md(session, stats, game_id))
    return out
