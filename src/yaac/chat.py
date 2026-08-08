"""The human client: a terminal chat that joins YAAC as an ordinary participant.

It does not speak MCP. `Backend` and `Membership` are used directly, which makes it a peer like any other -- and
means the README's central caveat does not apply here. "Messages do not arrive on their own" is a limitation of
MCP, which has no server-to-client message that reaches a model's context; this client holds its own DEALER, so
the hat pushes to it and `Membership.on_change` redraws. Nothing polls.

A long-running chat window is also a stable hat, so a net that includes one stops changing hands every time an
agent session exits.

The interface is modal, and the modes are arranged on one axis -- channels, chat, members -- so a single rule
covers navigation: left goes out, right goes in. An empty prompt is what frees the arrow keys to mean that, since
there is no cursor to move. Tab cycles the same axis and needs no empty prompt, so the roster stays reachable
mid-sentence. See docs/tui.md.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from typing import Any

from .backend import DEFAULT_ENDPOINT, Backend, ConnectionRefused, Membership, check_zmq_capabilities

try:
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.widgets import Input, Label, ListItem, ListView, RichLog, Static
except ModuleNotFoundError:  # pragma: no cover -- exercised by hand; the whole point is that it never reaches a UI
    sys.exit(
        "yaac-chat needs textual, which is an optional dependency.\n"
        '  uvx --from "yet-another-agentic-chat[chat]" yaac-chat\n'
        "or, in an existing environment:\n"
        "  pip install 'yet-another-agentic-chat[chat]'"
    )

EVERYONE = "everyone on the channel"


def stamp(iso: str | None) -> str:
    """Wire timestamps are ISO-8601 Zulu. Shown as local wall-clock, since the reader is a person in one place."""
    if not iso:
        return datetime.now().strftime("%H:%M")
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return "--:--"


class ChatApp(App):
    """One window, three modes, one membership at a time in this version."""

    CSS = """
    Screen { layout: vertical; }
    #log { height: 1fr; border: none; padding: 0 1; }
    #picker { height: auto; max-height: 12; display: none; border-top: solid $panel; }
    #picker.visible { display: block; }
    #prompt { border: none; padding: 0 1; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("tab", "cycle(1)", "Next mode", priority=True),
        Binding("shift+tab", "cycle(-1)", "Previous mode", priority=True),
        Binding("escape", "back", "Back", priority=True),
    ]

    # The navigation axis. Left goes out, right goes in; Tab walks the same order. Not named MODES:
    # Textual reserves that on App for its screen collection.
    AXIS = ("channels", "chat", "members")

    def __init__(self, endpoint: str, channel: str | None, name: str | None) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.wanted = (channel, name)
        self.radio = Backend(endpoint)
        self.membership: Membership | None = None
        self.recipient: str | None = None  # None means everyone; direct is the default once a peer is picked
        self.present: list[str] = []  # last roster seen, so the next one can be diffed into arrivals and departures
        self.mode = "chat"
        self._rows: list[tuple[str, str]] = []  # (kind, value) parallel to the picker's items

    # -- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", wrap=True, markup=True)
        yield Vertical(ListView(id="list"), id="picker")
        yield Input(placeholder="message, or press ← for channels and → for who is here", id="prompt")
        yield Static("", id="status")

    async def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        channel, name = self.wanted
        if channel and name:
            await self.join(channel, name)
        else:
            self.write("[dim]Not on a channel yet. Press ← to see what is on the air.[/dim]")
        self.refresh_status()

    # -- transport -------------------------------------------------------

    async def join(self, channel: str, name: str) -> None:
        try:
            result = await self.radio.connect(channel, name)
        except ConnectionRefused as exc:
            self.write(f"[red]Could not join {channel!r} as {name!r}: {exc}[/red]")
            return
        self.membership = self.radio.resolve(result.connection_id)
        # Redraw when the hat pushes, rather than asking it whether anything happened. The backend runs in this same
        # event loop, so the callback is an ordinary call and needs no thread hop.
        self.membership.on_change = self.drain
        self.present = self.membership.peer_names()
        self.recipient = None
        self.write(f"[dim]Joined [b]{channel}[/b] as [b]{name}[/b].[/dim]")
        if result.created:
            self.write(
                f"[yellow]Nobody was here, so this created {channel!r}. Check that is the name you meant.[/yellow]"
            )
        self.refresh_status()

    def drain(self) -> None:
        """Move whatever arrived into the transcript. Called from the receive loop, so it must not block or raise."""
        if self.membership is None:
            return
        for message in self.membership.receive():
            self.write(self.render_message(message))
        self.note_arrivals_and_departures()
        self.refresh_status()
        if self.mode == "members":
            self.show_members()

    def note_arrivals_and_departures(self) -> None:
        """Turn roster changes into transcript lines.

        The hat broadcasts a roster to every member on each hello and each eviction, so this is push, not polling.
        Writing presence into the history rather than a panel is what lets a single-column window answer "who is
        here" while you are reading: the answer is above you, with the time it changed. Departures arrive late by
        design -- eviction is lazy, discovered on the first failed send -- so a peer that died silently lingers
        until somebody writes to it.
        """
        if self.membership is None:
            return
        present = self.membership.peer_names()
        for name in [n for n in present if n not in self.present]:
            self.write(f"[dim]{stamp(None)}  → {name} is here[/dim]")
        for name in [n for n in self.present if n not in present]:
            self.write(f"[dim]{stamp(None)}  ← {name} left[/dim]")
            if name == self.recipient:
                self.recipient = None
                self.write(f"[dim]        you were writing to {name}; back to {EVERYONE}[/dim]")
        self.present = present

    def render_message(self, message: dict[str, Any]) -> str:
        if message.get("kind") == "bounce":
            return f"[red]{stamp(message.get('ts'))}  undelivered: {message.get('reason', 'no reason given')}[/red]"
        sender = (message.get("from") or {}).get("name") or "?"
        to = (message.get("to") or {}).get("name")
        # A broadcast and a whisper read identically otherwise, and they are not the same social act.
        addressed = "you" if to else "[i]all[/i]"
        return f"{stamp(message.get('ts'))}  [b]{sender}[/b] → {addressed}   {message.get('body', '')}"

    def write(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    # -- modes -----------------------------------------------------------

    def action_cycle(self, step: int) -> None:
        self.enter_mode(self.AXIS[(self.AXIS.index(self.mode) + step) % len(self.AXIS)])

    def action_back(self) -> None:
        self.enter_mode("chat")

    def enter_mode(self, mode: str) -> None:
        self.mode = mode
        picker = self.query_one("#picker")
        picker.set_class(mode != "chat", "visible")
        match mode:
            case "channels":
                self.run_worker(self.show_channels(), exclusive=True)
            case "members":
                self.show_members()
            case _:
                self.query_one("#prompt", Input).focus()
        self.refresh_status()

    def fill(self, rows: list[tuple[str, str, str]]) -> None:
        """Rows are (kind, value, label); kind and value are what activation acts on."""
        listing = self.query_one("#list", ListView)
        listing.clear()
        self._rows = [(kind, value) for kind, value, _ in rows]
        for _, _, label in rows:
            listing.append(ListItem(Label(label)))
        listing.focus()
        if rows:
            listing.index = 0

    async def show_channels(self) -> None:
        self.fill([("none", "", "  probing the net…")])
        channels = await self.radio.probe_channels()
        rows = [("join", "", "＋ join a channel…")]
        for entry in channels or []:
            name = entry["name"] or "(world)"
            here = " ← you are here" if self.membership and entry["name"] == self.membership.channel else ""
            rows.append(("channel", entry["name"], f"  {name}   [dim]{entry['count']} on air{here}[/dim]"))
        if not channels:
            rows.append(("none", "", "  [dim]nobody on the air[/dim]"))
        self.fill(rows)

    def show_members(self) -> None:
        rows = [("recipient", "", f"  {EVERYONE}   [dim]heard by everyone, costs everyone[/dim]")]
        for peer in self.membership.peers() if self.membership else []:
            rows.append(("recipient", peer.name or "", f"  {peer.name}"))
        if len(rows) == 1:
            rows.append(("none", "", "  [dim]nobody else on this channel[/dim]"))
        self.fill(rows)

    @on(ListView.Selected)
    def activate(self, event: ListView.Selected) -> None:
        kind, value = self._rows[event.list_view.index or 0]
        match kind:
            case "recipient":
                self.recipient = value or None
                self.enter_mode("chat")
            case "channel":
                self.run_worker(self.switch(value), exclusive=True)
            case "join":
                self.enter_mode("chat")
                self.write("[dim]Type: /join <channel> as <your name>[/dim]")
            case _:
                pass

    async def switch(self, channel: str) -> None:
        if self.membership and self.membership.channel == channel:
            self.enter_mode("chat")
            return
        self.enter_mode("chat")
        self.write(f"[dim]Type: /join {channel} as <your name>[/dim]")

    # -- input -----------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        """Arrows navigate only when there is no text to move through, which is what makes the gesture free."""
        prompt = self.query_one("#prompt", Input)
        if self.mode != "chat" or not prompt.has_focus or prompt.value:
            return
        match event.key:
            case "left":
                event.prevent_default()
                self.enter_mode("channels")
            case "right":
                event.prevent_default()
                self.enter_mode("members")

    @on(Input.Submitted, "#prompt")
    async def submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#prompt", Input).value = ""
        if not text:
            return
        if text.startswith("/"):
            await self.command(text)
            return
        if self.membership is None:
            self.write("[red]Not on a channel. Press ← to pick one.[/red]")
            return
        try:
            await self.membership.send(text, self.recipient)
        except RuntimeError as exc:
            self.write(f"[red]Not sent: {exc}[/red]")
            return
        self.write(f"{stamp(None)}  [b]you[/b] → {self.recipient or '[i]all[/i]'}   {text}")

    async def command(self, text: str) -> None:
        """Only what the modes cannot express yet: joining needs two free-text values at once."""
        match text.split():
            case ["/join", channel, "as", *rest] if rest:
                if self.membership is not None:
                    await self.radio.disconnect(self.membership.routing_id)
                    self.membership = None
                await self.join(channel, " ".join(rest))
            case ["/quit"]:
                self.exit()
            case _:
                self.write("[dim]/join <channel> as <name>   ·   /quit[/dim]")

    # -- chrome ----------------------------------------------------------

    def refresh_status(self) -> None:
        """The one piece of permanent chrome, so it carries what must be true at a glance: where you are, who you
        are about to interrupt, and who is listening. Names rather than a count, because a YAAC channel holds a
        handful of sessions, not a crowd."""
        if self.membership is None:
            self.query_one("#status", Static).update("not on a channel  ·  ← channels")
            return
        listening = ", ".join(self.present[:4]) + (f" +{len(self.present) - 4}" if len(self.present) > 4 else "")
        here = f"with {listening}" if self.present else "alone here"
        exits = "← channels · members →" if self.mode == "chat" else "esc to go back"
        self.query_one("#status", Static).update(
            f"{self.membership.channel} · you are {self.membership.name} · "
            f"to {self.recipient or EVERYONE} · {here}  ·  {exits}"
        )

    async def on_unmount(self) -> None:
        await self.radio.disconnect_all()
        self.radio.close()


def main() -> None:
    """Console entry point for `yaac-chat`. Unlike the MCP server, this one owns the terminal."""
    parser = argparse.ArgumentParser(
        prog="yaac-chat",
        description="Join a YAAC channel as a person, in a terminal window.",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Rendezvous endpoint (default: %(default)s).")
    parser.add_argument("--channel", help="Channel to join at startup. Without it, the channel list opens first.")
    parser.add_argument("--name", help="Your name on that channel. Required with --channel.")
    args = parser.parse_args()
    if bool(args.channel) != bool(args.name):
        parser.error("--channel and --name go together")

    try:
        check_zmq_capabilities()
    except RuntimeError as exc:
        raise SystemExit(f"[yaac] {exc}") from exc

    app = ChatApp(args.endpoint, args.channel, args.name)
    # zmq.asyncio needs loop.add_reader, which Windows's default ProactorEventLoop lacks. Same constraint as the
    # MCP server, same fix, but Textual owns the loop, so it is set before the app takes over.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    app.run()


if __name__ == "__main__":
    main()
