"""Command-line interface: ``pokernow stats|hands|hand|serve``."""

from __future__ import annotations

import argparse
import json
import sys

import os

from .models import ActionType, Street
from .parser import load_archive, parse_file
from .stats import compute_session_stats


def _load(path: str):
    """Load a CSV log, a JSON hands export, or an archive directory."""
    if os.path.isdir(path):
        return load_archive(path)
    return parse_file(path)


def _fmt(v, width: int = 6) -> str:
    if v is None:
        return "-".rjust(width)
    if isinstance(v, float):
        if v >= 999:
            return "inf".rjust(width)
        return f"{v:.1f}".rjust(width)
    return str(v).rjust(width)


def cmd_stats(args: argparse.Namespace) -> int:
    session = _load(args.file)
    stats = compute_session_stats(session)
    if args.json:
        print(json.dumps(stats.to_dict(), indent=2))
        return 0

    print(f"Session: {args.file}")
    print(
        f"Hands: {stats.hands}   Blinds: {stats.small_blind}/{stats.big_blind}   "
        f"Duration: {stats.duration_minutes} min   Avg pot: {stats.avg_pot}   "
        f"Biggest pot: {stats.biggest_pot} (hand #{stats.biggest_pot_hand})"
    )
    if stats.unparsed_lines:
        print(f"Note: {stats.unparsed_lines} log line(s) were not understood (see `pokernow unparsed`).")
    print()
    cols = ["Player", "Hands", "Net", "Net(bb)", "bb/100", "VPIP", "PFR", "3Bet", "AF", "WTSD", "W$SD", "CBet", "AllIn"]
    widths = [max(12, max(len(p.name) for p in stats.players) if stats.players else 12), 6, 8, 8, 8, 6, 6, 6, 6, 6, 6, 6, 6]
    print("  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(cols, widths))))
    print("  ".join("-" * w for w in widths))
    for p in stats.players:
        d = p.to_dict(stats.big_blind)
        row = [
            p.name.ljust(widths[0]),
            _fmt(p.hands, widths[1]),
            _fmt(p.net, widths[2]),
            _fmt(d["net_bb"], widths[3]),
            _fmt(d["bb_per_100"], widths[4]),
            _fmt(p.vpip, widths[5]),
            _fmt(p.pfr, widths[6]),
            _fmt(p.three_bet, widths[7]),
            _fmt(d["af"], widths[8]),
            _fmt(p.wtsd_pct, widths[9]),
            _fmt(p.wsd_pct, widths[10]),
            _fmt(p.cbet_pct, widths[11]),
            _fmt(p.all_ins, widths[12]),
        ]
        print("  ".join(row))
    return 0


def cmd_hands(args: argparse.Namespace) -> int:
    session = _load(args.file)
    stats = compute_session_stats(session)
    bb = stats.big_blind
    for h in session.hands:
        if args.player and not any(args.player in p for p in h.players):
            continue
        if h.pot < args.min_pot:
            continue
        winners = ", ".join(w.rsplit(" @ ", 1)[0] for w in h.winners)
        board = " ".join(h.board) if h.board else "-"
        pot_bb = f" ({h.pot / bb:.1f}bb)" if bb else ""
        print(f"#{h.number:<5} pot {h.pot:>7}{pot_bb:<10} board {board:<16} won by {winners}")
    return 0


def cmd_hand(args: argparse.Namespace) -> int:
    session = _load(args.file)
    for h in session.hands:
        if h.number == args.number:
            break
    else:
        print(f"hand #{args.number} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(h.to_dict(), indent=2))
        return 0
    print(f"Hand #{h.number} ({h.game_type}) dealer={h.dealer} at {h.started_at}")
    for s in h.seats:
        print(f"  seat {s.seat}: {s.player} ({s.stack})")
    if h.hero_cards:
        print(f"  Hero: {' '.join(h.hero_cards)}")
    board_for = {Street.FLOP: h.board[:3], Street.TURN: h.board[:4], Street.RIVER: h.board[:5]}
    for street in (Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER, Street.SHOWDOWN):
        acts = h.actions_on(street)
        board = board_for.get(street, [])
        if not acts and not board:
            continue
        print(f"  --- {street.value}{(' [' + ' '.join(board) + ']') if board else ''} ---")
        if not acts:
            print("  (no action)")
        for a in acts:
            _print_action(h, a)
    if len(h.board_runs) > 1:
        for i, run in enumerate(h.board_runs, 1):
            print(f"  run {i}: {' '.join(run)}")
    print(f"  Pot: {h.pot}   Net: " + ", ".join(f"{p.rsplit(' @ ',1)[0]} {h.net(p):+d}" for p in h.players))
    return 0


def _print_action(h, a) -> None:
    name = a.player.rsplit(" @ ", 1)[0]
    extra = ""
    if a.type is ActionType.RAISE:
        extra = f" to {a.to_amount}"
    elif a.amount is not None and a.type not in (ActionType.SHOW, ActionType.FOLD, ActionType.CHECK):
        extra = f" {a.amount}"
    if a.cards:
        extra += f" [{' '.join(a.cards)}]"
    if a.hand_desc:
        extra += f" ({a.hand_desc})"
    if a.all_in:
        extra += " ALL-IN"
    print(f"  {name}: {a.type.value}{extra}")


def cmd_unparsed(args: argparse.Namespace) -> int:
    session = _load(args.file)
    for line in session.unparsed:
        print(f"[session] {line}")
    for h in session.hands:
        for line in h.unparsed:
            print(f"[hand #{h.number}] {line}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from .fetch import FetchError, GameArchive, parse_game_ref

    try:
        _, game_id = parse_game_ref(args.game)
    except FetchError as e:
        print(e, file=sys.stderr)
        return 2
    what = tuple(w for w in ("ledger", "hands", "log") if not getattr(args, f"no_{w}", False))
    arch = GameArchive(game_id, args.data_dir)
    last = {"hands": -1, "log": -1}

    def progress(kind: str, done: int, total: int) -> None:
        if kind == "hands" and (done == total or done - last["hands"] >= 10):
            last["hands"] = done
            print(f"\r  hands {done}/{total}", end="", flush=True)
        elif kind == "log" and done - last["log"] >= 250:
            last["log"] = done
            print(f"\r  log lines {done}", end="", flush=True)

    print(f"Fetching game {game_id} -> {arch.dir}")
    backfill = getattr(args, "backfill", False)
    if backfill:
        import os as _os

        print("  backfill: re-downloading every hand and the full log (merged, no duplicates)")
        if not (_os.environ.get("POKERNOW_NPT") or _os.environ.get("POKERNOW_COOKIES")):
            print("  NOTE: POKERNOW_NPT is not set — a backfill without it will NOT add your own hole cards.")
    elif arch.exists():
        print("  existing archive found; refreshing incrementally")
    res = arch.refresh(args.game, what=what, progress=progress, log=lambda m: print(f"\n  {m}"),
                       force_hands=backfill, full_log=backfill)
    print()
    for w in res.warnings:
        print(f"  warning: {w}")
    for kind, path in arch.files().items():
        print(f"  {kind:6} {path}")
    if arch.exists():
        _write_exports(arch)
        print(f"Next: pokernow stats {arch.dir}   (or open the web UI and pick the game)")
    return 0


def _write_exports(arch) -> dict:
    from .export import write_exports

    session = load_archive(arch)
    out = write_exports(arch.dir, session, None, arch.game_id)
    print("  exports: " + ", ".join(os.path.basename(p) for p in out.values()))
    return out


def cmd_export(args: argparse.Namespace) -> int:
    from .fetch import GameArchive

    path = args.path
    if os.path.isdir(path):
        arch = GameArchive(os.path.basename(path.rstrip("/")), os.path.dirname(path.rstrip("/")))
        _write_exports(arch)
        return 0
    # single file: write next to it in a folder named after the file
    from .export import write_exports

    session = parse_file(path)
    out_dir = args.out or os.path.splitext(path)[0] + "-exports"
    out = write_exports(out_dir, session, None, None)
    print("\n".join(out.values()))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    import signal

    from .live import LiveTracker

    def on_update(t, res):
        st = t.state
        print(f"[{st.last_poll_at[11:19]}] {st.hands} hands, {st.log_lines} log lines  (+{res.new_hands} hands, +{res.new_log_lines} lines)")
        if t.stats:
            top = ", ".join(f"{p.name} {p.net:+d}" for p in t.stats.players[:6])
            print(f"           {top}")

    tracker = LiveTracker(args.game, data_dir=args.data_dir, interval=args.interval, on_update=on_update,
                          log=lambda m: print(f"           {m}"))
    print(f"Live-tracking {tracker.archive.game_id} every {args.interval:.0f}s -> {tracker.archive.dir}  (Ctrl-C to stop)")
    tracker.start()
    stop = {"flag": False}

    def handler(sig, frame):
        stop["flag"] = True
        tracker.stop()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    while not stop["flag"] and tracker.state.running:
        signal.pause() if hasattr(signal, "pause") else __import__("time").sleep(1)
    print("stopped.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.open:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")).start()
    uvicorn.run("pokernow.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pokernow", description="PokerNow.club hand-history tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="Per-player statistics for a log")
    s.add_argument("file")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("hands", help="List hands in a log")
    s.add_argument("file")
    s.add_argument("--player", help="Filter by player name substring")
    s.add_argument("--min-pot", type=int, default=0)
    s.set_defaults(fn=cmd_hands)

    s = sub.add_parser("hand", help="Replay one hand")
    s.add_argument("file")
    s.add_argument("number", type=int)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_hand)

    s = sub.add_parser("unparsed", help="Show log lines the parser did not understand")
    s.add_argument("file")
    s.set_defaults(fn=cmd_unparsed)

    s = sub.add_parser("fetch", help="Download a game's hands/log/ledger from a PokerNow game link (archived locally, incremental)")
    s.add_argument("game", help="Game URL (https://www.pokernow.com/games/<id>) or bare game id")
    s.add_argument("--data-dir", default=None, help="Archive root (default ~/.pokernow/games or $POKERNOW_DATA_DIR)")
    s.add_argument("--no-log", action="store_true", help="Skip the session log (hands are enough for stats)")
    s.add_argument("--no-hands", action="store_true")
    s.add_argument("--no-ledger", action="store_true")
    s.add_argument("--backfill", action="store_true",
                   help="Re-download everything (use after setting POKERNOW_NPT to add your own hole cards to an existing archive)")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("live", help="Live-track a running game: poll every N seconds and append to its archive")
    s.add_argument("game", help="Game URL or id")
    s.add_argument("--interval", type=float, default=15.0)
    s.add_argument("--data-dir", default=None)
    s.set_defaults(fn=cmd_live)

    s = sub.add_parser("export", help="(Re)generate stats.json / players.csv / hands.csv / summary.md for an archive or a file")
    s.add_argument("path", help="Archive directory, or a .csv/.json export")
    s.add_argument("--out", default=None, help="Output directory (single-file input only)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("serve", help="Run the web UI / API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.add_argument("--open", action="store_true", help="Open the UI in the default browser")
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
