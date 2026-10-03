"""Stream Director Discord adapter with fake Discord objects: card + thread,
posts, persistent buttons and modals, team checks, mention safety, restart."""

import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))
_ISOLATED = tempfile.TemporaryDirectory()
os.environ["DARKABYSS_DATA_DIR"] = str(Path(_ISOLATED.name) / "data")
for _name in ("stream_director", "stream_director_config", "stream_director_store", "stream_director_discord"):
    sys.modules.pop(_name, None)
import discord  # noqa: E402

sd = importlib.import_module("stream_director")
sdc = importlib.import_module("stream_director_config")
sds = importlib.import_module("stream_director_store")
sdd = importlib.import_module("stream_director_discord")

GUILD = 123456789012345678
CHANNEL = 223456789012345678
ROLE = 323456789012345678
MOD_ROLE = 423456789012345678
START = 1_791_000_000.0


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class FakeMessage:
    _next = 900

    def __init__(self, channel, content=None, **kwargs):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.channel = channel
        self.content = content
        self.kwargs = kwargs
        self.edits = []
        self.thread = None

    async def create_thread(self, name, auto_archive_duration):
        self.thread = FakeChannel(self.id + 50_000, name=name, guild=self.channel.guild, client=self.channel.client)
        self.channel.client.channels[self.thread.id] = self.thread
        return self.thread


class FakePartial:
    def __init__(self, channel, message_id):
        self.channel = channel
        self.message_id = message_id

    async def edit(self, **kwargs):
        self.channel.edits.append((self.message_id, kwargs))


class Permissions:
    def __init__(self, **values):
        self.values = values

    def __getattr__(self, name):
        return self.values.get(name, True)


class FakeChannel:
    def __init__(self, channel_id, name="stream", guild=None, client=None, permissions=None):
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.client = client
        self.sent = []
        self.edits = []
        self.archived = False
        self._permissions = permissions or Permissions()

    async def send(self, content=None, **kwargs):
        message = FakeMessage(self, content, **kwargs)
        self.sent.append(message)
        return message

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    def permissions_for(self, _member):
        return self._permissions

    async def edit(self, **kwargs):
        self.edits.append(("channel", kwargs))


class FakeClient:
    def __init__(self):
        self.channels = {}
        self.guild = types.SimpleNamespace(id=GUILD, name="Home", me=object(), owner_id=1)
        self.channel = FakeChannel(CHANNEL, guild=self.guild, client=self)
        self.channels[CHANNEL] = self.channel

    def get_channel(self, channel_id):
        return self.channels.get(int(channel_id))

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"), "missing")


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.modals = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self._done = True
        self.modals.append(modal)


def member(user_id, *, manage=False, roles=()):
    return types.SimpleNamespace(
        id=user_id,
        display_name=f"user{user_id}",
        guild=types.SimpleNamespace(owner_id=1),
        guild_permissions=types.SimpleNamespace(manage_guild=manage, administrator=False),
        roles=[types.SimpleNamespace(id=role) for role in roles],
    )


def interaction(custom, user, components=None, guild_id=GUILD):
    data = {"custom_id": custom}
    if components is not None:
        data["components"] = components
    return types.SimpleNamespace(data=data, user=user, guild_id=guild_id, response=FakeResponse(), followup=types.SimpleNamespace(send=None))


class AdapterCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "data"
        self.clock = Clock()
        self.client = FakeClient()
        self.build()

    def build(self, **changes):
        config = sdc.parse_config(
            sdc.normalize_config({"guild_id": str(GUILD), "channel_id": str(CHANNEL), "team_role_ids": [str(MOD_ROLE)], "suggestion_cooldown_seconds": 0, **changes})
        )
        self.director = sd.Director(sds.StateStore(self.data_dir), config, self.clock)
        self.front = sdd.StreamDirectorDiscord(self.client, self.director, twitch_login=lambda: "streamer", clock=self.clock, log=lambda _t: None)

    async def live(self):
        await self.front.apply(self.director.handle_event(sd.StreamEvent("online", "m-on", self.clock(), {"stream_id": "s1", "started_at": START})))
        return self.director.session

    def thread(self):
        return self.client.channels[int(self.director.session["discord"]["thread_id"])]


class PostingTests(AdapterCase):
    async def test_stream_start_and_end_are_announced_to_the_other_bots(self):
        import bot_events

        events_dir = Path(self.temp.name) / "events"
        self.front.events = bot_events.EventPublisher("sd-main", "stream_director", events_dir, clock=self.clock)
        session = await self.live()
        self.clock.now += 600
        await self.front.apply(self.director.manual_end(sd.Actor("discord", "1", "owner", True)).effects)
        events = bot_events.read_file(events_dir / "sd-main.json")
        self.assertEqual([event.kind for event in events], ["stream.started", "stream.ended"])
        started = events[0]
        self.assertEqual((started.guild_id, started.channel_id), (GUILD, int(session["discord"]["thread_id"])))
        self.assertEqual(started.message_id, int(session["discord"]["card_message_id"]))
        self.assertEqual(started.source_type, "stream_director")

    async def test_card_thread_and_mention_safety(self):
        self.build(go_live_role_id=str(ROLE))
        session = await self.live()
        card = self.client.channel.sent[0]
        self.assertIn(f"<@&{ROLE}>", card.content)
        mentions = card.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertEqual([role.id for role in mentions.roles], [ROLE])
        self.assertFalse(mentions.users)
        self.assertIn("LIVE", card.kwargs["embed"].title)
        self.assertEqual({item.custom_id for item in card.kwargs["view"].children}, {"sd:card:moment", "sd:card:challenge", "sd:card:suggest"})
        refs = session["discord"]
        self.assertEqual(int(refs["card_message_id"]), card.id)
        thread = self.thread()
        self.assertTrue(thread.name.startswith("🔴"))
        # Every other post: no mentions at all, user text escaped.
        outcome = self.director.suggest_challenge(sd.Actor("discord", "5", "@everyone"), "@everyone [win](https://evil.example)")
        await self.front.apply(outcome.effects)
        posts = thread.sent[1:]
        challenge_post = posts[-1]
        self.assertEqual(challenge_post.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertNotIn("@everyone", challenge_post.content)
        self.assertNotIn("](https", challenge_post.content)
        for message in thread.sent:
            self.assertEqual(message.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_useful_events_only_and_the_recap(self):
        await self.live()
        thread = self.thread()
        before = len(thread.sent)
        for index in range(5):  # follows/subs/cheers are counted, never posted one by one
            await self.front.apply(self.director.handle_event(sd.StreamEvent("sub", f"sub-{index}", self.clock(), {})))
        self.assertEqual(len(thread.sent), before)
        await self.front.apply(self.director.handle_event(sd.StreamEvent("raid", "raid", self.clock(), {"from_name": "Pal", "viewers": 3})))
        self.assertIn("Raid from **Pal**", thread.sent[-1].content)
        self.clock.now += 3600
        await self.front.apply(self.director.manual_end(sd.Actor("discord", "1", "owner", True)).effects)
        recap = thread.sent[-1].kwargs["embed"]
        self.assertTrue(recap.title.startswith("📼 Recap"))
        self.assertIn("Stream", [field.name for field in recap.fields])
        card_edit = self.client.channel.edits[-1][1]
        self.assertIsNone(card_edit["view"])  # the card becomes the short recap, buttons gone
        self.assertIn("ended", card_edit["embed"].title)
        self.assertTrue(any(kind == "channel" and kwargs.get("name", "").startswith("📼") for kind, kwargs in thread.edits))

    async def test_card_edits_are_debounced(self):
        await self.live()
        actor = sd.Actor("discord", "7", "a")
        for index in range(3):
            self.clock.now += 40
            await self.front.apply(self.director.mark_moment(sd.Actor("discord", str(10 + index), "x")).effects)
        edits = len(self.client.channel.edits)
        await self.front.apply(self.director.mark_moment(actor).effects)
        self.assertEqual(len(self.client.channel.edits), edits)  # within 20 s: wait
        self.clock.now += 21
        await self.front.flush()
        self.assertEqual(len(self.client.channel.edits), edits + 1)
        self.assertFalse(self.director.session["card_dirty"])

    async def test_polls_post_debounce_and_close(self):
        await self.live()
        team = sd.Actor("discord", "1", "owner", True)
        outcome = self.director.create_poll(team, "poll", "Next?", ["A", "B"], minutes=1)
        await self.front.apply(outcome.effects)
        thread = self.thread()
        poll_message = thread.sent[-1]
        self.assertEqual(len(poll_message.kwargs["view"].children), 2)
        poll_id = outcome.effects[0].ref
        await self.front.apply(self.director.vote(sd.Actor("discord", "8", "v"), poll_id, 1).effects)
        self.assertEqual(thread.edits, [])
        self.clock.now += 5
        await self.front.flush()
        self.assertEqual(len(thread.edits), 1)
        self.clock.now += 60
        await self.front.apply(self.director.tick())
        last_edit = thread.edits[-1][1]
        self.assertIsNone(last_edit["view"])
        self.assertIn("closed", last_edit["embed"].title)

    async def test_missing_permissions_and_channel(self):
        self.client.channel._permissions = Permissions(create_public_threads=False, embed_links=False)
        problem = self.front.check_channel()
        self.assertIn("Create Public Threads", problem)
        self.assertIn("Embed Links", problem)
        del self.client.channels[CHANNEL]
        self.assertIn("not visible", self.front.check_channel())
        # A session still runs; posts are skipped without errors.
        await self.live()
        self.assertIsNotNone(self.director.session)


class InteractionTests(AdapterCase):
    async def test_card_buttons_open_modals_and_submissions_are_handled(self):
        await self.live()
        viewer = member(50)
        click = interaction("sd:card:moment", viewer)
        self.assertTrue(await self.front.handle_interaction(click))
        self.assertEqual(click.response.modals[0].custom_id, "sd:modal:moment")
        submit = interaction("sd:modal:moment", viewer, [{"type": 1, "components": [{"type": 4, "custom_id": "comment", "value": "what a save"}]}])
        await self.front.handle_interaction(submit)
        text, kwargs = submit.response.messages[0]
        self.assertIn("Moment saved", text)
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(self.director.session["moments"][0]["comment"], "what a save")
        # Newer modal layout (label components) is read too.
        suggest = interaction("sd:modal:suggest", viewer, [{"type": 18, "component": {"type": 4, "custom_id": "text", "value": "Play Celeste?"}}])
        await self.front.handle_interaction(suggest)
        self.assertIn("inbox", suggest.response.messages[0][0])
        self.assertEqual(self.director.inbox_counts(), {"question": 1})

    async def test_team_only_buttons(self):
        await self.live()
        outcome = self.director.suggest_challenge(sd.Actor("discord", "50", "viewer"), "Speedrun the tutorial")
        await self.front.apply(outcome.effects)
        challenge_id = outcome.effects[0].ref
        viewer_click = interaction(f"sd:ch:{challenge_id}:accept", member(51))
        await self.front.handle_interaction(viewer_click)
        self.assertIn("Only the stream team", viewer_click.response.messages[0][0])
        support = interaction(f"sd:ch:{challenge_id}:support", member(51))
        await self.front.handle_interaction(support)
        self.assertIn("support", support.response.messages[0][0])
        for team_member in (member(60, manage=True), member(61, roles=[MOD_ROLE])):
            self.assertTrue(sdd.actor_for(team_member, self.director.config).team)
        mod_click = interaction(f"sd:ch:{challenge_id}:accept", member(61, roles=[MOD_ROLE]))
        await self.front.handle_interaction(mod_click)
        self.assertEqual(self.director.state["challenges"][challenge_id]["status"], "accepted")
        edit = self.thread().edits[-1][1]
        self.assertEqual({item.custom_id for item in edit["view"].children}, {f"sd:ch:{challenge_id}:{action}" for action in ("support", "complete", "fail")})

    async def test_buttons_survive_a_restart(self):
        await self.live()
        team = sd.Actor("discord", "1", "owner", True)
        poll_id = self.director.create_poll(team, "prediction", "Win?", ["Yes", "No"], minutes=5).effects[0].ref
        # The bot restarts: a fresh director and adapter from the same files.
        self.build()
        vote = interaction(f"sd:poll:{poll_id}:vote:0", member(70))
        await self.front.handle_interaction(vote)
        self.assertIn("Yes", vote.response.messages[0][0])
        resolve = interaction(f"sd:poll:{poll_id}:resolve:0", member(60, manage=True))
        await self.front.handle_interaction(resolve)
        self.assertEqual(self.director.state["polls"][poll_id]["outcome"], 0)

    async def test_foreign_or_broken_interactions(self):
        self.assertFalse(await self.front.handle_interaction(interaction("gp:mute", member(1))))
        self.assertFalse(await self.front.handle_interaction(interaction(None, member(1))))
        other = interaction("sd:card:moment", member(1), guild_id=999)
        self.assertTrue(await self.front.handle_interaction(other))
        self.assertEqual(other.response.messages[0][0], sdd.UNAVAILABLE)
        outdated = interaction("sd:poll:5:vote:notanumber", member(1))
        await self.front.handle_interaction(outdated)
        self.assertIn("outdated", outdated.response.messages[0][0])
        (self.data_dir / sds.STATE_FILE_NAME).write_text("{", encoding="utf-8")
        self.build()
        broken = interaction("sd:card:suggest", member(1))
        await self.front.handle_interaction(broken)
        self.assertEqual(broken.response.messages[0][0], sdd.UNAVAILABLE)

    async def test_inbox_view_for_the_team(self):
        self.director.suggest(sd.Actor("discord", "5", "v"), "game", "Hades")
        items = self.director.inbox_items()
        view = sdd.inbox_components(items)
        self.assertEqual([item.custom_id for item in view.children], [f"sd:inbox:{items[0]['id']}:done", f"sd:inbox:{items[0]['id']}:dismissed"])
        click = interaction(view.children[0].custom_id, member(60, manage=True))
        await self.front.handle_interaction(click)
        self.assertEqual(self.director.inbox_counts(), {})


class HelperTests(unittest.TestCase):
    def test_parse_when(self):
        now = datetime(2026, 10, 3, 18, 0).timestamp()
        self.assertEqual(sdd.parse_when("in 2h", now), now + 7200)
        self.assertEqual(sdd.parse_when("in 1d 30m", now), now + 86400 + 1800)
        self.assertEqual(sdd.parse_when("20:30", now), datetime(2026, 10, 3, 20, 30).timestamp())
        self.assertEqual(sdd.parse_when("17:00", now), datetime(2026, 10, 4, 17, 0).timestamp())
        self.assertEqual(sdd.parse_when("tomorrow 19:00", now), datetime(2026, 10, 4, 19, 0).timestamp())
        self.assertEqual(sdd.parse_when("2026-10-10 21:15", now), datetime(2026, 10, 10, 21, 15).timestamp())
        self.assertIsNone(sdd.parse_when("someday", now))
        self.assertIsNone(sdd.parse_when("25:00", now))

    def test_custom_ids_and_guess_kind(self):
        self.assertEqual(sdd.parse_custom_id("sd:ch:12:support"), ["ch", "12", "support"])
        self.assertIsNone(sdd.parse_custom_id("other:1"))
        self.assertIsNone(sdd.parse_custom_id("sd:" + "x" * 200))
        self.assertEqual(sdd.guess_kind("When is the next stream?", None), "question")
        self.assertEqual(sdd.guess_kind("look", "https://clips.twitch.tv/x"), "clip")
        self.assertEqual(sdd.guess_kind("talk about pets", None), "topic")

    def test_slash_commands_are_registered(self):
        async def build():
            client = discord.Client(intents=discord.Intents.none())
            tree = discord.app_commands.CommandTree(client)
            temp = tempfile.TemporaryDirectory()
            director = sd.Director(sds.StateStore(Path(temp.name)), sdc.StreamDirectorConfig(), lambda: START)
            sdd.StreamDirectorDiscord(client, director).build_commands(tree)
            names = {command.name for command in tree.get_commands()}
            await client.close()
            temp.cleanup()
            return names

        names = asyncio.run(build())
        self.assertEqual(names, {"moment", "challenge", "suggest", "poll", "prediction", "inbox", "community", "stream", "goal", "nextstream"})


if __name__ == "__main__":
    unittest.main()
