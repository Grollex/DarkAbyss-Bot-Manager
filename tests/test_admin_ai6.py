"""AI-6: expanded Discord-only Admin Tools, guards, blueprint, bot features and
the two-stage (planner -> executor) AI transport. No real Discord connection."""

import asyncio
import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))


def load(name):
    return importlib.import_module(name)


admin_tools = load("admin_tools")
admin_features = load("admin_features")
admin_blueprint = load("admin_blueprint")
admin_tools_server = load("admin_tools_server")

OWNER_ID = 1
MOD_ID = 2
USER_ID = 3
HIGH_ID = 4
BOT_ID = 9


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeRole:
    def __init__(self, role_id, name, position, permissions=0, *, default=False, managed=False):
        self.id = role_id
        self.name = name
        self.position = position
        self.permissions = discord.Permissions(permissions)
        self.managed = managed
        self._default = default
        self.members = []
        self.edits = []
        self.deleted = False

    def is_default(self):
        return self._default

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "permissions" in kwargs:
            self.permissions = kwargs["permissions"]

    async def delete(self, *, reason=None):
        self.deleted = True

    def __str__(self):
        return self.name


class FakeMember:
    def __init__(self, member_id, name, roles, everyone, *, permissions=0, bot=False):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.mention = f"<@{member_id}>"
        self._roles = list(roles)
        self.everyone = everyone
        self.guild_permissions = discord.Permissions(permissions)
        self.bot = bot
        self.voice = None
        self.joined_at = None
        self.calls = []
        self.guild = None

    @property
    def roles(self):
        return [self.everyone, *self._roles]

    @property
    def top_role(self):
        return max(self.roles, key=lambda role: role.position)

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            if role not in self._roles:
                self._roles.append(role)
        self.calls.append(("add_roles", [role.id for role in roles]))

    async def remove_roles(self, *roles, reason=None):
        for role in roles:
            if role in self._roles:
                self._roles.remove(role)
        self.calls.append(("remove_roles", [role.id for role in roles]))

    async def edit(self, **kwargs):
        self.calls.append(("edit", kwargs))

    async def ban(self, *, reason=None, delete_message_seconds=0):
        self.calls.append(("ban", delete_message_seconds))

    async def kick(self, *, reason=None):
        self.calls.append(("kick",))

    async def timeout(self, until, *, reason=None):
        self.calls.append(("timeout", until))

    async def move_to(self, channel, *, reason=None):
        self.calls.append(("move_to", getattr(channel, "id", None)))

    def __str__(self):
        return self.name


class FakeMessage:
    _next = 5000

    def __init__(self, channel, content=None, **kwargs):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.channel = channel
        self.content = content
        self.kwargs = kwargs
        self.author = channel.guild.me if channel.guild else None
        self.deleted = False
        self.edits = []
        self.reactions = []

    async def delete(self):
        self.deleted = True

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def pin(self, *, reason=None):
        self.pinned = True

    async def unpin(self, *, reason=None):
        self.pinned = False


class FakeChannel:
    def __init__(self, guild, channel_id, name, channel_type="text", category=None):
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.type = channel_type
        self.category = category
        self.parent = category
        self.position = 0
        self.overwrites = {}
        self.sent = []
        self.messages = {}
        self.edits = []
        self.deleted = False
        self.channels = []
        self.available_tags = []

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())

    async def set_permissions(self, target, *, overwrite=None, reason=None):
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite

    async def send(self, content=None, **kwargs):
        message = FakeMessage(self, content, **kwargs)
        self.sent.append(message)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "missing")
        return self.messages[message_id]

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]

    async def delete(self, *, reason=None):
        self.deleted = True
        if self in self.guild.channels:
            self.guild.channels.remove(self)

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self):
        self.id = 10
        self.name = "Test Server"
        self.owner_id = OWNER_ID
        self.member_count = 4
        self.features = []
        self.premium_tier = 0
        self.default_role = FakeRole(10, "@everyone", 0, discord.Permissions.general().value, default=True)
        self.r_member = FakeRole(20, "Member", 1)
        self.r_mod = FakeRole(30, "Mod", 5, discord.Permissions(manage_messages=True, kick_members=True).value)
        self.r_high = FakeRole(40, "High", 8)
        self.r_bot = FakeRole(90, "Kairo", 10, discord.Permissions.all().value & ~8, managed=True)
        self.roles = [self.default_role, self.r_member, self.r_mod, self.r_high, self.r_bot]
        self.channels = []
        self.next_id = 1000
        self.general = self.add_channel("general")
        self.voice = self.add_channel("Voice", "voice")
        everyone = self.default_role
        self._members = {}
        self.owner = self.add_member(FakeMember(OWNER_ID, "owner", [], everyone, permissions=discord.Permissions.all().value))
        self.mod = self.add_member(FakeMember(MOD_ID, "mod", [self.r_mod], everyone, permissions=discord.Permissions(manage_messages=True, kick_members=True, manage_roles=True).value))
        self.user = self.add_member(FakeMember(USER_ID, "user", [self.r_member], everyone))
        self.high = self.add_member(FakeMember(HIGH_ID, "high", [self.r_high], everyone))
        self.me = self.add_member(FakeMember(BOT_ID, "Kairo", [self.r_bot], everyone, bot=True))
        self.edits = []
        self.created = []
        self.hooks = []
        self.rules = []

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def add_channel(self, name, channel_type="text", category=None):
        channel = FakeChannel(self, self.new_id(), name, channel_type, category)
        self.channels.append(channel)
        if category is not None:
            category.channels.append(channel)
        return channel

    def add_member(self, member):
        member.guild = self
        self._members[member.id] = member
        return member

    @property
    def members(self):
        return list(self._members.values())

    def get_member(self, member_id):
        return self._members.get(member_id)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_channel(self, channel_id):
        return next((channel for channel in self.channels if channel.id == channel_id), None)

    get_channel_or_thread = get_channel

    async def create_role(self, *, name, reason=None, **kwargs):
        role = FakeRole(self.new_id(), name, 1, getattr(kwargs.get("permissions"), "value", 0))
        role.kwargs = kwargs
        self.roles.append(role)
        self.created.append(("role", name, kwargs))
        return role

    async def _create(self, kind, name, category=None, **kwargs):
        channel = self.add_channel(name, kind, category)
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        channel.kwargs = kwargs
        self.created.append((kind, name, kwargs))
        return channel

    async def create_category(self, name, *, reason=None, **kwargs):
        return await self._create("category", name, **kwargs)

    async def create_text_channel(self, name, *, reason=None, category=None, news=False, **kwargs):
        return await self._create("news" if news else "text", name, category, **kwargs)

    async def create_voice_channel(self, name, *, reason=None, category=None, **kwargs):
        return await self._create("voice", name, category, **kwargs)

    async def create_stage_channel(self, name, *, reason=None, category=None, **kwargs):
        return await self._create("stage_voice", name, category, **kwargs)

    async def create_forum(self, name, *, reason=None, category=None, **kwargs):
        return await self._create("forum", name, category, **kwargs)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def webhooks(self):
        return self.hooks

    async def fetch_automod_rules(self):
        return self.rules

    async def create_automod_rule(self, **kwargs):
        rule = SimpleNamespace(id=self.new_id(), **kwargs)
        self.rules.append(rule)
        return rule


class MemoryStore(admin_features.FeatureStore):
    def __init__(self, path):
        super().__init__(path)


def context(guild, requester=OWNER_ID, *, enforce=True, store=None, attachments=None):
    return admin_tools.AdminToolContext(
        guild=guild,
        source="/ai",
        requesting_user_id=requester,
        suppress_mentions=True,
        enforce_hierarchy=enforce,
        feature_store=store,
        attachments=attachments,
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# registry, schemas, validator
# ---------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_every_tool_is_categorized_and_in_the_catalog(self):
        catalog = admin_tools.render_tool_catalog()
        for name, definition in admin_tools.TOOL_DEFINITIONS.items():
            with self.subTest(tool=name):
                self.assertIn(definition.category, admin_tools.TOOL_CATEGORIES)
                self.assertIn(f"- {name}", catalog)
                self.assertEqual(definition.kind == "read", definition.risk == "read")
                self.assertIn(name, admin_tools._TOOL_HANDLERS)
        self.assertGreaterEqual(len(admin_tools.TOOL_DEFINITIONS), 90)
        # Catalog stays small (names + one-liners only) for low-TPM providers.
        self.assertLess(len(catalog), 12000)
        self.assertNotIn('"properties"', catalog)

    def test_execute_keeps_the_fixed_legacy_action_set(self):
        self.assertEqual(len(admin_tools.EXECUTE_TOOL_NAMES), 17)
        self.assertLessEqual(len(admin_tools.EXECUTE_TOOL_NAMES), 25)
        for name in admin_tools.EXECUTE_TOOL_NAMES:
            self.assertEqual(admin_tools.get_tool_definition(name).kind, "write")
        self.assertNotIn("apply_server_blueprint", admin_tools.EXECUTE_TOOL_NAMES)

    def test_reimport_registers_extensions_again(self):
        sys.modules.pop("admin_tools", None)
        fresh = importlib.import_module("admin_tools")
        self.assertIn("apply_server_blueprint", fresh.TOOL_DEFINITIONS)
        self.assertIn("create_role_menu", fresh._TOOL_HANDLERS)
        with self.assertRaises(fresh.AdminToolError):
            fresh.register_tool(fresh.TOOL_DEFINITIONS["send_embed"], fresh._TOOL_HANDLERS["send_embed"])

    def test_no_host_or_network_surface_in_new_tool_modules(self):
        forbidden = ["subprocess", "shell=True", "os.system", "eval(", "exec(", "urlopen", "requests.", "aiohttp", "open("]
        for module in ("admin_tools_server.py", "admin_tools_content.py", "admin_blueprint.py"):
            source = (CORE_ROOT / module).read_text(encoding="utf-8")
            for pattern in forbidden:
                with self.subTest(module=module, pattern=pattern):
                    self.assertNotIn(pattern, source)

    def test_webhook_tokens_never_leave_the_tools(self):
        source = (CORE_ROOT / "admin_tools_content.py").read_text(encoding="utf-8")
        self.assertNotIn('"token":', source)
        self.assertNotIn('"url": getattr(hook', source)


class ValidatorTests(unittest.TestCase):
    def test_nested_schema_validation(self):
        validate = admin_tools.validate_tool_arguments
        good = {"blueprint": {"roles": [{"name": "Gamer", "color": "#00FF00", "permissions": ["send_messages"]}]}}
        self.assertEqual(validate("apply_server_blueprint", good), good)
        bad_cases = [
            {"blueprint": {"roles": [{"name": "x", "color": "green"}]}},
            {"blueprint": {"roles": [{"name": "x", "unexpected": 1}]}},
            {"blueprint": {"channels": [{"name": "a", "type": "telepathy"}]}},
            {"blueprint": {"channels": [{"type": "text"}]}},
            {"blueprint": {"roles": "not-a-list"}},
        ]
        for arguments in bad_cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises((ValueError, admin_tools.AdminToolError)):
                    validate("apply_server_blueprint", arguments)

    def test_enum_is_type_strict_and_arrays_bounded(self):
        validate = admin_tools.validate_tool_arguments
        with self.assertRaises(ValueError):
            validate("edit_guild", {"afk_timeout_seconds": True})
        with self.assertRaises(ValueError):
            validate("edit_guild", {"afk_timeout_seconds": 61})
        validate("edit_guild", {"afk_timeout_seconds": 300})
        with self.assertRaises(ValueError):
            validate("create_role", {"name": "x", "permissions": ["send_messages", "send_messages"]})
        with self.assertRaises(ValueError):
            validate("create_poll", {"channel_id": "1", "question": "q", "answers": []})


# ---------------------------------------------------------------------------
# anti-escalation guards
# ---------------------------------------------------------------------------


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild()

    def test_non_owner_cannot_touch_roles_at_or_above_their_top_role(self):
        result = run(admin_tools.execute_tool(context(self.guild, MOD_ID), "add_role", {"member_id": str(USER_ID), "role_id": "40"}))
        self.assertFalse(result.ok)
        self.assertIn("above your highest role", result.message)
        result = run(admin_tools.execute_tool(context(self.guild, MOD_ID), "add_role", {"member_id": str(USER_ID), "role_id": "20"}))
        self.assertTrue(result.ok, result.message)

    def test_non_owner_cannot_moderate_higher_members_owner_or_bot(self):
        for target in (HIGH_ID, OWNER_ID, BOT_ID):
            with self.subTest(target=target):
                result = run(admin_tools.execute_tool(context(self.guild, MOD_ID), "ban_member", {"member_id": str(target)}))
                self.assertFalse(result.ok)
        self.assertEqual(self.guild.high.calls, [])
        ok = run(admin_tools.execute_tool(context(self.guild, MOD_ID), "kick_member", {"member_id": str(USER_ID)}))
        self.assertTrue(ok.ok, ok.message)

    def test_permissions_are_limited_to_what_the_requester_holds(self):
        denied = run(admin_tools.execute_tool(context(self.guild, MOD_ID), "create_role", {"name": "x", "permissions": ["ban_members"]}))
        self.assertFalse(denied.ok)
        self.assertIn("ban_members", denied.message)
        allowed = run(admin_tools.execute_tool(context(self.guild, MOD_ID), "create_role", {"name": "y", "permissions": ["kick_members"]}))
        self.assertTrue(allowed.ok, allowed.message)

    def test_administrator_is_never_granted_even_for_the_owner(self):
        for tool, arguments in (
            ("create_role", {"name": "x", "permissions": ["administrator"]}),
            ("edit_role", {"role_id": "20", "add_permissions": ["administrator"]}),
            ("set_channel_permissions", {"channel_id": str(self.guild.general.id), "target_type": "role", "target_id": "20", "allow": ["administrator"]}),
        ):
            with self.subTest(tool=tool):
                result = run(admin_tools.execute_tool(context(self.guild, OWNER_ID), tool, arguments))
                self.assertFalse(result.ok)
                self.assertIn("Administrator", result.message)

    def test_execute_context_keeps_legacy_semantics(self):
        legacy = admin_tools.AdminToolContext(guild=self.guild, source="/execute", requesting_user_id=MOD_ID)
        result = run(admin_tools.execute_tool(legacy, "add_role", {"member_id": str(USER_ID), "role_id": "40"}))
        self.assertTrue(result.ok, result.message)
        # Bot hierarchy still applies to everyone.
        bot_role = run(admin_tools.execute_tool(legacy, "delete_role", {"role_id": "90"}))
        self.assertFalse(bot_role.ok)

    def test_unknown_permission_names_are_explained(self):
        result = run(admin_tools.execute_tool(context(self.guild), "create_role", {"name": "x", "permissions": ["fly"]}))
        self.assertFalse(result.ok)
        self.assertIn("view_channel", result.message)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = admin_features.FeatureStore(Path(self.temp.name) / "features.json")

    def call(self, tool, arguments, requester=OWNER_ID, **kwargs):
        kwargs.setdefault("store", self.store)
        return run(admin_tools.execute_tool(context(self.guild, requester, **kwargs), tool, arguments))

    def test_private_category_and_channel_creation(self):
        result = self.call("create_category", {"name": "Staff", "private_to_role_ids": ["30"]})
        self.assertTrue(result.ok, result.message)
        category = self.guild.channels[-1]
        self.assertFalse(category.overwrites[self.guild.default_role].view_channel)
        self.assertTrue(category.overwrites[self.guild.r_mod].view_channel)
        self.assertTrue(category.overwrites[self.guild.me].view_channel)
        text = self.call("create_text_channel", {"name": "mod-chat", "category_id": result.data["category_id"], "topic": "t", "slowmode_seconds": 5})
        self.assertTrue(text.ok, text.message)
        self.assertIs(self.guild.channels[-1].category, category)
        self.assertEqual(text.data["channel_id"], str(self.guild.channels[-1].id))

    def test_set_channel_permissions_allow_deny_reset_clear(self):
        channel_id = str(self.guild.general.id)
        result = self.call("set_channel_permissions", {"channel_id": channel_id, "target_type": "everyone", "deny": ["send_messages"], "allow": ["view_channel"]})
        self.assertTrue(result.ok, result.message)
        overwrite = self.guild.general.overwrites[self.guild.default_role]
        self.assertFalse(overwrite.send_messages)
        self.assertTrue(overwrite.view_channel)
        self.call("set_channel_permissions", {"channel_id": channel_id, "target_type": "everyone", "reset": ["send_messages"]})
        self.assertIsNone(self.guild.general.overwrites[self.guild.default_role].send_messages)
        conflict = self.call("set_channel_permissions", {"channel_id": channel_id, "target_type": "everyone", "allow": ["speak"], "deny": ["speak"]})
        self.assertFalse(conflict.ok)
        cleared = self.call("set_channel_permissions", {"channel_id": channel_id, "target_type": "everyone", "clear": True})
        self.assertTrue(cleared.ok)
        self.assertNotIn(self.guild.default_role, self.guild.general.overwrites)

    def test_edit_role_only_checks_newly_granted_permissions(self):
        result = self.call("edit_role", {"role_id": "30", "color": "#112233", "remove_permissions": ["kick_members"]}, requester=OWNER_ID)
        self.assertTrue(result.ok, result.message)
        edit = self.guild.r_mod.edits[-1]
        self.assertFalse(edit["permissions"].kick_members)
        self.assertTrue(edit["permissions"].manage_messages)
        self.assertEqual(edit["colour"].value, 0x112233)
        both = self.call("edit_role", {"role_id": "30", "permissions": ["speak"], "add_permissions": ["connect"]})
        self.assertFalse(both.ok)

    def test_bulk_update_role_respects_the_cap(self):
        for index in range(5):
            self.guild.add_member(FakeMember(100 + index, f"m{index}", [], self.guild.default_role))
        refused = self.call("bulk_update_role", {"role_id": "20", "action": "add", "max_members": 2})
        self.assertFalse(refused.ok)
        self.assertIn("nothing was changed", refused.message)
        done = self.call("bulk_update_role", {"role_id": "20", "action": "add", "max_members": 50})
        self.assertTrue(done.ok, done.message)
        self.assertTrue(all(self.guild.r_member in member.roles for member in self.guild.members if not member.bot))

    def test_lockdown_and_restore_use_the_feature_store(self):
        everyone = self.guild.default_role
        before = everyone.permissions.value
        locked = self.call("lockdown_server", {})
        self.assertTrue(locked.ok, locked.message)
        self.assertFalse(everyone.permissions.send_messages)
        again = self.call("lockdown_server", {})
        self.assertFalse(again.ok)
        restored = self.call("end_lockdown", {})
        self.assertTrue(restored.ok, restored.message)
        self.assertEqual(everyone.permissions.value, before)
        self.assertIsNone(self.store.get(self.guild.id, "lockdown"))
        no_store = self.call("lockdown_server", {}, store=None)
        self.assertFalse(no_store.ok)

    def test_edit_guild_maps_enums_and_clear_fields(self):
        result = self.call("edit_guild", {"verification_level": "high", "clear_fields": ["system_channel"], "name": "New"})
        self.assertTrue(result.ok, result.message)
        edit = self.guild.edits[-1]
        self.assertEqual(edit["verification_level"], discord.VerificationLevel.high)
        self.assertIsNone(edit["system_channel"])
        self.assertEqual(edit["name"], "New")
        empty = self.call("edit_guild", {})
        self.assertFalse(empty.ok)

    def test_guild_icon_only_from_request_attachments(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64

        class Attachment:
            id = 777
            content_type = "image/png"
            size = len(png)

            async def read(self):
                return png

        missing = self.call("set_guild_icon", {"attachment_id": "777"})
        self.assertFalse(missing.ok)
        ok = self.call("set_guild_icon", {"attachment_id": "777"}, attachments={"777": Attachment()})
        self.assertTrue(ok.ok, ok.message)
        self.assertEqual(self.guild.edits[-1]["icon"], png)

        class Fake(Attachment):
            content_type = "image/png"

            async def read(self):
                return b"not an image at all"

        fake = self.call("set_guild_icon", {"attachment_id": "777"}, attachments={"777": Fake()})
        self.assertFalse(fake.ok)

    def test_send_embed_poll_and_bot_message_edit(self):
        channel_id = str(self.guild.general.id)
        sent = self.call("send_embed", {"channel_id": channel_id, "embed": {"title": "Rules", "description": "Be nice", "color": "#FF0000", "fields": [{"name": "1", "value": "x"}]}})
        self.assertTrue(sent.ok, sent.message)
        message = self.guild.general.sent[-1]
        self.assertEqual(message.kwargs["embed"].title, "Rules")
        self.assertEqual(message.kwargs["allowed_mentions"].everyone, False)
        edited = self.call("edit_bot_message", {"channel_id": channel_id, "message_id": sent.data["message_id"], "content": "updated"})
        self.assertTrue(edited.ok, edited.message)
        poll = self.call("create_poll", {"channel_id": channel_id, "question": "Best game?", "answers": [{"text": "A"}, {"text": "B"}], "duration_hours": 2})
        self.assertTrue(poll.ok, poll.message)
        self.assertEqual(len(self.guild.general.sent[-1].kwargs["poll"].answers), 2)

    def test_automod_rules_are_built_from_validated_arguments(self):
        result = self.call("create_automod_rule", {
            "name": "No slurs", "trigger": "keyword_preset", "presets": ["profanity", "slurs"],
            "alert_channel_id": str(self.guild.general.id), "exempt_role_ids": ["30"],
        })
        self.assertTrue(result.ok, result.message)
        rule = self.guild.rules[-1]
        self.assertEqual(rule.trigger.presets.value, 5)
        self.assertEqual([action.type for action in rule.actions], [discord.AutoModRuleActionType.block_message, discord.AutoModRuleActionType.send_alert_message])
        self.assertEqual(rule.exempt_roles, [self.guild.r_mod])
        keyword = self.call("create_automod_rule", {"name": "Links", "trigger": "keyword", "keywords": ["*discord.gg*"], "timeout_seconds": 60})
        self.assertTrue(keyword.ok, keyword.message)
        spam_timeout = self.call("create_automod_rule", {"name": "Spam", "trigger": "spam", "timeout_seconds": 60})
        self.assertFalse(spam_timeout.ok)
        empty = self.call("create_automod_rule", {"name": "Empty", "trigger": "keyword"})
        self.assertFalse(empty.ok)

    def test_webhook_listing_never_exposes_tokens(self):
        self.guild.hooks = [SimpleNamespace(id=55, name="hook", channel_id=self.guild.general.id, user=self.guild.me, token="SECRET_TOKEN", url="https://discord.com/api/webhooks/55/SECRET_TOKEN")]
        result = self.call("list_webhooks", {})
        self.assertTrue(result.ok)
        self.assertNotIn("SECRET_TOKEN", json.dumps(result.data))
        self.assertTrue(result.data["webhooks"][0]["usable_by_bot"])


# ---------------------------------------------------------------------------
# blueprint
# ---------------------------------------------------------------------------


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = admin_features.FeatureStore(Path(self.temp.name) / "features.json")
        self.blueprint = {
            "roles": [{"name": "Gamer", "color": "#3366FF", "hoist": True}, {"name": "Member"}],
            "categories": [
                {"name": "Info", "channels": [{"name": "rules", "type": "text"}, {"name": "announcements", "type": "announcement"}]},
                {"name": "Staff", "private_to_roles": ["Mod"], "channels": [{"name": "mod chat", "type": "text"}]},
            ],
            "channels": [{"name": "general", "type": "text"}, {"name": "Lobby", "type": "voice", "user_limit": 5}],
        }

    def call(self, tool, arguments, requester=OWNER_ID):
        return run(admin_tools.execute_tool(context(self.guild, requester, store=self.store), tool, arguments))

    def test_check_reports_create_and_reuse_without_changes(self):
        result = self.call("check_server_blueprint", {"blueprint": self.blueprint})
        self.assertTrue(result.ok, result.message)
        counts = result.data["counts"]
        self.assertEqual(counts["roles_to_create"], 1)  # Member already exists
        self.assertEqual(counts["categories_to_create"], 2)
        self.assertEqual(counts["channels_to_create"], 4)  # general already exists
        self.assertEqual(counts["reused_existing"], 2)
        self.assertEqual(self.guild.created, [])

    def test_apply_creates_only_missing_objects_and_undo_removes_them(self):
        result = self.call("apply_server_blueprint", {"blueprint": self.blueprint})
        self.assertTrue(result.ok, result.message)
        kinds = [item[0] for item in self.guild.created]
        self.assertEqual(kinds.count("role"), 1)
        self.assertEqual(kinds.count("category"), 2)
        staff = next(channel for channel in self.guild.channels if channel.name == "Staff")
        self.assertFalse(staff.overwrites[self.guild.default_role].view_channel)
        self.assertTrue(staff.overwrites[self.guild.r_mod].view_channel)
        self.assertIn("news", [channel.type for channel in self.guild.channels])
        record = self.store.get(self.guild.id, "last_blueprint")
        self.assertEqual(len(record["created"]["channels"]), 4)

        # Applying again creates nothing new.
        before = len(self.guild.created)
        again = self.call("apply_server_blueprint", {"blueprint": self.blueprint})
        self.assertTrue(again.ok)
        self.assertEqual(len(self.guild.created), before)

        undo = self.call("undo_last_blueprint", {})
        self.assertTrue(undo.ok, undo.message)
        self.assertNotIn("Staff", [channel.name for channel in self.guild.channels])
        self.assertIn("general", [channel.name for channel in self.guild.channels])
        gamer = next(role for role in self.guild.roles if role.name == "Gamer")
        self.assertTrue(gamer.deleted)

    def test_unknown_role_reference_and_escalation_are_rejected(self):
        bad = dict(self.blueprint, categories=[{"name": "X", "private_to_roles": ["Ghost"]}])
        result = self.call("apply_server_blueprint", {"blueprint": bad})
        self.assertFalse(result.ok)
        self.assertIn("Ghost", result.message)
        escalate = {"roles": [{"name": "Boss", "permissions": ["ban_members"]}]}
        result = self.call("apply_server_blueprint", {"blueprint": escalate}, requester=MOD_ID)
        self.assertFalse(result.ok)
        self.assertEqual(self.guild.created, [])


# ---------------------------------------------------------------------------
# persistent bot features
# ---------------------------------------------------------------------------


class FakeInteractionResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False

    def is_done(self):
        return self.deferred or bool(self.sent)

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeFollowup:
    def __init__(self, response):
        self.response = response

    async def send(self, content, **kwargs):
        self.response.sent.append((content, kwargs))


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "admin_features.json"
        self.store = admin_features.FeatureStore(self.path)
        original = admin_features.MEMBER_TYPES
        admin_features.MEMBER_TYPES = (FakeMember,)
        self.addCleanup(setattr, admin_features, "MEMBER_TYPES", original)

    def call(self, tool, arguments, requester=OWNER_ID):
        return run(admin_tools.execute_tool(context(self.guild, requester, store=self.store), tool, arguments))

    def click(self, custom_id, member):
        response = FakeInteractionResponse()
        interaction = SimpleNamespace(
            data={"custom_id": custom_id}, guild=self.guild, user=member, response=response, followup=FakeFollowup(response)
        )
        handled = run(admin_features.handle_component_interaction(interaction, self.store))
        # Every handled click is acknowledged before any slow role change.
        self.assertTrue(response.deferred)
        return handled, response.sent

    def test_store_is_atomic_json_and_survives_reload(self):
        self.store.set(self.guild.id, "welcome", {"enabled": True})
        reloaded = admin_features.FeatureStore(self.path)
        self.assertEqual(reloaded.get(self.guild.id, "welcome"), {"enabled": True})
        self.store.set(self.guild.id, "welcome", None)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["guilds"], {})

    def test_corrupt_store_is_kept_aside(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = admin_features.FeatureStore(self.path)
        self.assertIsNone(store.get(1, "x"))
        store.set(1, "x", 1)
        self.assertTrue(any(item.name.startswith("admin_features.corrupt-") for item in self.path.parent.iterdir()))

    def test_role_menu_toggle_and_dangerous_roles(self):
        refused = self.call("create_role_menu", {"channel_id": str(self.guild.general.id), "title": "Roles", "options": [{"role_id": "30"}]})
        self.assertFalse(refused.ok)  # Mod has moderator permissions
        created = self.call("create_role_menu", {"channel_id": str(self.guild.general.id), "title": "Roles", "options": [{"role_id": "20", "label": "Member"}]})
        self.assertTrue(created.ok, created.message)
        menu_id = created.data["menu_id"]
        message = self.guild.general.sent[-1]
        custom_ids = [item.custom_id for item in message.kwargs["view"].children]
        self.assertEqual(custom_ids, [f"dab:rm:{menu_id}:20"])
        member = self.guild.add_member(FakeMember(200, "newbie", [], self.guild.default_role))
        handled, replies = self.click(custom_ids[0], member)
        self.assertTrue(handled)
        self.assertIn(self.guild.r_member, member.roles)
        self.assertIn("Added role", replies[-1][0])
        self.assertTrue(replies[-1][1]["ephemeral"])
        self.click(custom_ids[0], member)
        self.assertNotIn(self.guild.r_member, member.roles)
        # Forged button for a role that is not in the menu.
        handled, replies = self.click(f"dab:rm:{menu_id}:40", member)
        self.assertTrue(handled)
        self.assertNotIn(self.guild.r_high, member.roles)
        # Role later given moderator permissions is no longer handed out.
        self.guild.r_member.permissions = discord.Permissions(ban_members=True)
        self.click(custom_ids[0], member)
        self.assertNotIn(self.guild.r_member, member.roles)
        self.assertFalse(run(admin_features.handle_component_interaction(SimpleNamespace(data={"custom_id": "other"}), self.store)))

    def test_verification_and_welcome_on_join(self):
        verified = self.call("setup_verification", {"channel_id": str(self.guild.general.id), "verified_role_id": "20"})
        self.assertTrue(verified.ok, verified.message)
        welcome = self.call("set_welcome", {"channel_id": str(self.guild.general.id), "message": "Hi {user} in {server}!", "auto_role_ids": ["20"]})
        self.assertTrue(welcome.ok, welcome.message)
        member = self.guild.add_member(FakeMember(300, "joiner", [], self.guild.default_role))
        run(admin_features.handle_member_join(member, self.store))
        self.assertIn(self.guild.r_member, member.roles)
        text = self.guild.general.sent[-1]
        self.assertEqual(text.content, "Hi <@300> in Test Server!")
        mentions = text.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        disabled = self.call("set_welcome", {"enabled": False})
        self.assertTrue(disabled.ok)

    def test_scheduled_messages_post_when_due_without_mentions(self):
        created = self.call("create_scheduled_message", {"channel_id": str(self.guild.general.id), "messages": ["@everyone ERROR 404"], "interval_minutes": 10, "start_in_minutes": 0, "max_posts": 1})
        self.assertTrue(created.ok, created.message)
        client = SimpleNamespace(get_guild=lambda guild_id: self.guild if guild_id == self.guild.id else None)
        posted = run(admin_features.run_due_schedules(client, self.store, now=time.time() + 1))
        self.assertEqual(posted, 1)
        message = self.guild.general.sent[-1]
        self.assertFalse(message.kwargs["allowed_mentions"].everyone)
        self.assertEqual(self.store.get(self.guild.id, "schedules"), None)  # max_posts reached
        too_fast = self.call("create_scheduled_message", {"channel_id": str(self.guild.general.id), "messages": ["x"], "interval_minutes": 1})
        self.assertFalse(too_fast.ok)

    def test_list_bot_features(self):
        self.call("create_scheduled_message", {"channel_id": str(self.guild.general.id), "messages": ["a", "b"], "interval_minutes": 60})
        result = self.call("list_bot_features", {})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["scheduled_messages"][0]["variants"], 2)


# ---------------------------------------------------------------------------
# two-stage AI transport
# ---------------------------------------------------------------------------


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin_ai = load("admin_ai")
        self.ai_platform = load("ai_platform")
        self.ai_orchestrator = load("ai_orchestrator")
        self.guild = FakeGuild()
        original = self.admin_ai.MEMBER_TYPES
        self.admin_ai.MEMBER_TYPES = (FakeMember,)
        self.addCleanup(setattr, self.admin_ai, "MEMBER_TYPES", original)

    def result(self, content, status="COMPLETED", **kwargs):
        return self.ai_orchestrator.OrchestratorResult(status=self.ai_orchestrator.OrchestratorStatus(status), content=content, **kwargs)

    def transport(self, results, *, planning=True):
        calls = []
        outer = self

        class Orchestrator:
            async def orchestrate(self, request, *, executor=None, confirmation_policy=None):
                calls.append((request, executor))
                return results.pop(0)

        transport = self.admin_ai.AITransport(
            load_config=lambda: {"ai_allowed_user_ids": [OWNER_ID], "ai_allowed_role_ids": []},
            orchestrator_factory=lambda: Orchestrator(),
            planning=planning,
            feature_store="STORE",
        )
        return transport, calls

    async def run_request(self, transport, prompt, attachments=None):
        sent = []

        class Delivery:
            mode = self.admin_ai.DeliveryMode.EPHEMERAL

            async def send(self, content, *, view=None):
                sent.append(content)

        binding = self.admin_ai.RequestBinding(OWNER_ID, self.guild.id, 1)
        source = SimpleNamespace(guild=self.guild, user=self.guild.owner)
        await transport._run_request(delivery=Delivery(), source=source, binding=binding, prompt=prompt, task_mode="routine", attachments=attachments)
        return sent

    async def test_planner_answer_mode_skips_the_executor(self):
        transport, calls = self.transport([self.result('{"mode":"answer","answer":"Привет!"}', provider_id="gemini", model_id="g")])
        sent = await self.run_request(transport, "привет")
        self.assertEqual(len(calls), 1)
        planner_request = calls[0][0]
        self.assertEqual(planner_request.task_class.value, "PLANNER")
        self.assertEqual(planner_request.allowed_tool_names, ())
        self.assertIn("Tool catalog", planner_request.messages[0].content)
        self.assertIn("apply_server_blueprint", planner_request.messages[0].content)
        self.assertTrue(sent[0].startswith("Привет!"))

    async def test_act_mode_runs_executor_with_only_planned_tools(self):
        plan = {"mode": "act", "tools": ["apply_server_blueprint", "not_a_tool"], "steps": ["build the gaming server"]}
        transport, calls = self.transport([
            self.result("```json\n" + json.dumps(plan) + "\n```", provider_id="gemini", model_id="gem"),
            self.result("done", provider_id="groq", model_id="oss"),
        ])
        sent = await self.run_request(transport, "сделай игровой сервер")
        self.assertEqual(len(calls), 2)
        executor_request, executor = calls[1]
        self.assertEqual(executor_request.task_class.value, "ROUTINE")
        names = executor_request.allowed_tool_names
        self.assertIn("apply_server_blueprint", names)
        self.assertIn("list_channels", names)  # core reads always available
        self.assertNotIn("not_a_tool", names)
        self.assertNotIn("ban_member", names)
        self.assertIn("build the gaming server", executor_request.messages[0].content)
        self.assertIn("plan: Gemini", sent[0])
        self.assertIn("run: Groq", sent[0])

    async def test_missing_planner_profile_falls_back_to_routine_planning(self):
        transport, calls = self.transport([
            self.result(None, status="UNAVAILABLE"),
            self.result('{"mode":"answer","answer":"ok"}'),
        ])
        await self.run_request(transport, "hi")
        self.assertEqual([request.task_class.value for request, _ in calls], ["PLANNER", "ROUTINE"])

    async def test_unusable_plan_falls_back_to_keyword_routing(self):
        transport, calls = self.transport([self.result("I think we should..."), self.result("done")])
        await self.run_request(transport, "забань спамера и очисти чат")
        names = calls[1][0].allowed_tool_names
        self.assertIn("ban_member", names)
        self.assertIn("purge_messages", names)
        self.assertLess(len(names), len(admin_tools.TOOL_DEFINITIONS))

    async def test_attachments_are_described_and_reach_tool_context(self):
        attachment = SimpleNamespace(id=4242, filename="logo.png", content_type="image/png", size=1234)
        transport, calls = self.transport([self.result('{"mode":"act","tools":["set_guild_icon"],"steps":["set icon"]}'), self.result("done")])
        await self.run_request(transport, "поставь иконку", attachments=self.admin_ai.collect_attachments([attachment]))
        user_message = calls[1][0].messages[-1].content
        self.assertIn("attachment_id=4242", user_message)
        captured = {}
        original = admin_tools.execute_tool

        async def fake_execute(context, tool_name, arguments=None):
            captured["context"] = context
            return admin_tools.ToolResult(True, tool_name, "ok")

        self.admin_ai.admin_tools.execute_tool = fake_execute
        self.addCleanup(setattr, self.admin_ai.admin_tools, "execute_tool", original)
        await calls[1][1]("get_guild_summary", {})
        context_used = captured["context"]
        self.assertTrue(context_used.enforce_hierarchy)
        self.assertEqual(list(context_used.attachments), ["4242"])
        self.assertEqual(context_used.feature_store, "STORE")

    def test_blueprint_outline_is_readable_and_mention_safe(self):
        call = SimpleNamespace(public_dict=lambda: {
            "tool_name": "apply_server_blueprint",
            "risk": "NORMAL",
            "arguments": {"blueprint": {
                "roles": [{"name": "Gamer"}, {"name": "@everyone`x"}],
                "categories": [{"name": "Staff", "private_to_roles": ["Mod"], "channels": [{"name": "mod-chat", "type": "text"}]}],
                "channels": [{"name": "Lobby", "type": "voice"}],
            }},
        })
        outline = self.admin_ai.render_plan_outline([call])
        self.assertIn("Roles: Gamer", outline)
        self.assertIn("Category Staff (private: Mod): mod-chat [text]", outline)
        self.assertIn("Without category: Lobby [voice]", outline)
        self.assertNotIn("`", outline)
        self.assertEqual(self.admin_ai.render_plan_outline([SimpleNamespace(public_dict=lambda: {"tool_name": "send_message", "arguments": {}})]), "")

    def test_parse_plan_is_strict(self):
        parse = self.admin_ai.parse_plan
        self.assertIsNone(parse("no json"))
        self.assertIsNone(parse('{"mode":"answer","answer":"  "}'))
        self.assertIsNone(parse('{"mode":"other"}'))
        plan = parse('{"mode":"act","tools":["send_message",5,"x"],"steps":["a"],"notes":"n"}')
        self.assertEqual(plan["tools"], ["send_message"])


class AdminWiringTests(unittest.TestCase):
    def test_ai_command_accepts_files_and_feature_listeners_exist(self):
        sys.modules.setdefault("msvcrt", SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda *a: None))
        sys.modules.pop("Admin", None)
        admin = importlib.import_module("Admin")
        ai_command = admin.bot.tree.get_command("ai")
        names = [parameter.name for parameter in ai_command.parameters]
        self.assertEqual(names, ["prompt", "mode", "file", "file2"])
        self.assertTrue(admin.ai_transport.planning)
        listeners = {name for name in admin.bot.extra_events}
        self.assertIn("on_interaction", listeners)
        self.assertIn("on_member_join", listeners)
        self.assertIn("data_dir", admin.AdminRuntime.__dataclass_fields__)
        execute = admin.bot.tree.get_command("execute")
        action = next(parameter for parameter in execute.parameters if parameter.name == "action")
        self.assertEqual(len(action.choices), 17)


if __name__ == "__main__":
    unittest.main()
