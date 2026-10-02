import asyncio
import importlib
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"

# Never touch the real user data folder: modules imported by these tests
# resolve app_paths from DARKABYSS_DATA_DIR (AI storage, instances, migration).
import os as _os  # noqa: E402
import tempfile as _tempfile  # noqa: E402

if not _os.environ.get("DARKABYSS_DATA_DIR"):
    _os.environ["DARKABYSS_DATA_DIR"] = _tempfile.mkdtemp(prefix="darkabyss-test-")


def load_admin_tools():
    sys.path.insert(0, str(CORE_ROOT))
    sys.modules.pop("admin_tools", None)
    return importlib.import_module("admin_tools")


class FakePermissions:
    def __init__(self, value=0, administrator=False):
        self.value = value
        self.administrator = administrator


class FakeRole:
    def __init__(self, role_id, name, *, position=0, managed=False, permissions=0):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self.color = "#123456"
        self.permissions = FakePermissions(permissions)

    async def delete(self, *, reason=None):
        self.deleted_reason = reason


class FakeOverwrite:
    send_messages = None

    def pair(self):
        return FakePermissions(1024), FakePermissions(2048)


class FakeHistory:
    def __init__(self, messages):
        self.messages = messages

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for message in self.messages:
            yield message


class FakeChannel:
    def __init__(self, channel_id, name, *, channel_type="text", parent=None):
        self.id = channel_id
        self.name = name
        self.type = channel_type
        self.position = 3
        self.parent = parent
        self.category = parent
        self.overwrites = {}
        self.sent = []
        self.purged = []
        self.permission_changes = []
        self.history_limit = None
        self.history_messages = []

    async def send(self, content):
        self.sent.append(content)

    async def purge(self, *, limit, reason=None):
        self.purged.append((limit, reason))
        return [object() for _ in range(limit)]

    async def edit(self, *, name, reason=None):
        self.old_name = self.name
        self.name = name
        self.edit_reason = reason

    async def delete(self, *, reason=None):
        self.deleted_reason = reason

    def overwrites_for(self, role):
        self.overwrite_role = role
        return FakeOverwrite()

    async def set_permissions(self, role, *, overwrite, reason=None):
        self.permission_changes.append((role, overwrite.send_messages, reason))

    def history(self, *, limit):
        self.history_limit = limit
        return FakeHistory(self.history_messages[:limit])


class FakeMember:
    def __init__(self, member_id, name, *, roles=None, administrator=False):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.roles = roles or []
        self.guild_permissions = FakePermissions(64, administrator=administrator)
        self.timed_out_until = None
        self.calls = []

    async def timeout(self, until, *, reason=None):
        self.timed_out_until = until
        self.calls.append(("timeout", until, reason))

    async def kick(self, *, reason=None):
        self.calls.append(("kick", reason))

    async def ban(self, *, reason=None, delete_message_seconds=0):
        self.calls.append(("ban", reason, delete_message_seconds))

    async def add_roles(self, role, *, reason=None):
        self.calls.append(("add_roles", role.id, reason))

    async def remove_roles(self, role, *, reason=None):
        self.calls.append(("remove_roles", role.id, reason))

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self):
        self.id = 10
        self.name = "Guild"
        self.member_count = 42
        self.default_role = FakeRole(1, "@everyone")
        self.role = FakeRole(200, "Mod", position=2, permissions=4096)
        self.member = FakeMember(300, "Alice", roles=[self.role])
        self.category = FakeChannel(90, "Category", channel_type="category")
        self.text_channel = FakeChannel(100, "general", parent=self.category)
        self.voice_channel = FakeChannel(101, "voice", channel_type="voice")
        self.text_channel.overwrites = {self.role: FakeOverwrite()}
        self.channels = [self.category, self.text_channel, self.voice_channel]
        self.roles = [self.default_role, self.role]
        self.members = [self.member]
        self.created_text_channels = []
        self.created_voice_channels = []
        self.created_roles = []
        self.unbanned = []

    def get_channel(self, channel_id):
        return next((channel for channel in self.channels if channel.id == channel_id), None)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_member(self, member_id):
        return next((member for member in self.members if member.id == member_id), None)

    async def create_text_channel(self, *, name, reason=None):
        channel = FakeChannel(500, name)
        self.created_text_channels.append((channel, reason))
        return channel

    async def create_voice_channel(self, *, name, reason=None):
        channel = FakeChannel(501, name, channel_type="voice")
        self.created_voice_channels.append((channel, reason))
        return channel

    async def create_role(self, *, name, reason=None):
        role = FakeRole(600, name)
        self.created_roles.append((role, reason))
        return role

    async def unban(self, user, *, reason=None):
        self.unbanned.append((user, reason))


class AdminToolRegistryTests(unittest.TestCase):
    def test_known_tools_registered_with_deterministic_metadata(self):
        admin_tools = load_admin_tools()
        names = [definition.name for definition in admin_tools.list_tool_definitions()]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("get_guild_summary", names)
        self.assertIn("send_message", names)
        self.assertEqual(admin_tools.get_tool_definition("get_recent_messages").kind, "read")
        self.assertEqual(admin_tools.get_tool_definition("purge_messages").risk, "destructive")
        self.assertEqual(admin_tools.get_tool_definition("send_message").risk, "normal")

    def test_unknown_tool_rejected(self):
        admin_tools = load_admin_tools()
        with self.assertRaises(admin_tools.AdminToolError):
            admin_tools.get_tool_definition("not_registered")

        result = asyncio.run(
            admin_tools.execute_tool(admin_tools.AdminToolContext(guild=FakeGuild()), "not_registered", {})
        )
        self.assertFalse(result.ok)
        self.assertIn("Unknown admin tool", result.message)

    def test_execute_choices_map_to_registered_write_tools(self):
        sys.path.insert(0, str(CORE_ROOT))
        sys.modules.pop("Admin", None)
        sys.modules.pop("admin_tools", None)
        admin_tools = importlib.import_module("admin_tools")
        Admin = importlib.import_module("Admin")
        self.assertEqual([choice.value for choice in Admin.ACTION_CHOICES], list(admin_tools.EXECUTE_TOOL_NAMES))

    def test_tool_schemas_are_provider_neutral_json_schema(self):
        admin_tools = load_admin_tools()
        forbidden = {"Member", "Role", "TextChannel", "VoiceChannel", "discord-snowflake"}
        for definition in admin_tools.list_tool_definitions():
            with self.subTest(tool=definition.name):
                json.dumps(definition.arguments)
                self.assertEqual(definition.arguments["type"], "object")
                self.assertIsInstance(definition.arguments["properties"], dict)
                self.assertIsInstance(definition.arguments["required"], list)
                self.assertIs(definition.arguments["additionalProperties"], False)
                schema_text = json.dumps(definition.arguments)
                self.assertFalse(any(token in schema_text for token in forbidden))

    def test_provider_schema_and_validation_helpers_are_data_only(self):
        admin_tools = load_admin_tools()
        schema = admin_tools.get_provider_tool_schema("send_message")
        self.assertEqual(schema["name"], "send_message")
        self.assertIn("content", schema["arguments"]["properties"])
        schema["arguments"]["properties"]["content"]["description"] = "mutated"
        self.assertNotEqual(
            admin_tools.get_provider_tool_schema("send_message")["arguments"]["properties"]["content"]["description"],
            "mutated",
        )

        schemas = admin_tools.list_provider_tool_schemas(("list_channels", "send_message"))
        self.assertEqual([item["name"] for item in schemas], ["list_channels", "send_message"])
        json.dumps(schemas)

        validated = admin_tools.validate_tool_arguments(
            "send_message",
            {"channel_id": "123", "content": "hello", "reason": None},
        )
        self.assertEqual(validated["channel_id"], "123")
        with self.assertRaises(admin_tools.AdminToolError):
            admin_tools.validate_tool_arguments("send_message", {"channel_id": "123", "content": "hello", "extra": "no"})
        with self.assertRaises(admin_tools.AdminToolError):
            admin_tools.validate_tool_arguments("send_message", {"content": "hello"})
        with self.assertRaises(ValueError):
            admin_tools.validate_tool_arguments("send_message", {"channel_id": "abc", "content": "hello"})
        with self.assertRaises(admin_tools.AdminToolError):
            admin_tools.get_provider_tool_schema("not_registered")

    def test_registry_and_handler_keys_match_exactly(self):
        admin_tools = load_admin_tools()
        self.assertEqual(set(admin_tools.TOOL_DEFINITIONS), set(admin_tools._TOOL_HANDLERS))


class AdminToolValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_snowflakes_and_boolean_snowflakes_rejected(self):
        admin_tools = load_admin_tools()
        for value in (True, False, 0, -1, "abc", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    admin_tools.parse_snowflake(value, "user_id")

    async def test_malformed_arguments_rejected_without_calling_tool(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        context = admin_tools.AdminToolContext(guild=guild)

        result = await admin_tools.execute_tool(context, "send_message", {"channel_id": "100", "content": ""})
        self.assertFalse(result.ok)
        self.assertEqual(guild.text_channel.sent, [])

        object_result = await admin_tools.execute_tool(
            context,
            "send_message",
            {"channel_id": guild.text_channel, "content": "hello"},
        )
        self.assertFalse(object_result.ok)
        self.assertIn("JSON-compatible", object_result.message)

        callable_result = await admin_tools.execute_tool(
            context,
            "send_message",
            {"channel_id": lambda: None, "content": "hello"},
        )
        self.assertFalse(callable_result.ok)
        self.assertIn("JSON-compatible", callable_result.message)

        unknown = await admin_tools.execute_tool(
            context,
            "send_message",
            {"channel_id": "100", "content": "hello", "python_object": "nope"},
        )
        self.assertFalse(unknown.ok)
        self.assertIn("Unknown argument", unknown.message)

        missing = await admin_tools.execute_tool(context, "send_message", {"content": "hello"})
        self.assertFalse(missing.ok)
        self.assertIn("Missing required argument", missing.message)

        invalid_snowflake = await admin_tools.execute_tool(
            context,
            "send_message",
            {"channel_id": "abc", "content": "hello"},
        )
        self.assertFalse(invalid_snowflake.ok)

        bool_snowflake = await admin_tools.execute_tool(
            context,
            "send_message",
            {"channel_id": True, "content": "hello"},
        )
        self.assertFalse(bool_snowflake.ok)

    async def test_string_maximums_are_enforced_locally(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        context = admin_tools.AdminToolContext(guild=guild)

        too_long_message = await admin_tools.execute_tool(
            context,
            "send_message",
            {"channel_id": "100", "content": "x" * (admin_tools.MAX_DISCORD_MESSAGE_LENGTH + 1)},
        )
        self.assertFalse(too_long_message.ok)
        self.assertIn("or fewer", too_long_message.message)

        too_long_name = await admin_tools.execute_tool(
            context,
            "create_role",
            {"name": "x" * (admin_tools.MAX_DISCORD_NAME_LENGTH + 1)},
        )
        self.assertFalse(too_long_name.ok)
        self.assertIn("or fewer", too_long_name.message)

        too_long_reason = await admin_tools.execute_tool(
            context,
            "create_role",
            {"name": "ok", "reason": "x" * (admin_tools.MAX_REASON_LENGTH + 1)},
        )
        self.assertFalse(too_long_reason.ok)
        self.assertIn("or fewer", too_long_reason.message)

    async def test_numeric_minimums_and_maximums_are_enforced_locally(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        context = admin_tools.AdminToolContext(guild=guild)

        purge_too_many = await admin_tools.execute_tool(context, "purge_messages", {"channel_id": "100", "count": 101})
        self.assertFalse(purge_too_many.ok)
        self.assertIn("100 or lower", purge_too_many.message)

        timeout_too_long = await admin_tools.execute_tool(
            context,
            "timeout_member",
            {"member_id": "300", "duration_minutes": 40321},
        )
        self.assertFalse(timeout_too_long.ok)
        self.assertIn("40320 or lower", timeout_too_long.message)

    async def test_existing_write_tool_delegation_behavior(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        context = admin_tools.AdminToolContext(guild=guild)
        result = await admin_tools.execute_tool(
            context,
            "send_message",
            {"channel_id": "100", "content": "hello"},
        )
        self.assertTrue(result.ok)
        self.assertEqual(guild.text_channel.sent, ["hello"])
        self.assertEqual(result.message, "Message sent to #general.")

    async def test_member_and_role_operations_resolve_ids_internally(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        context = admin_tools.AdminToolContext(guild=guild)

        timeout = await admin_tools.execute_tool(
            context,
            "timeout_member",
            {"member_id": "300", "duration_minutes": 5},
        )
        self.assertTrue(timeout.ok)
        self.assertEqual(guild.member.calls[-1][0], "timeout")

        add_role = await admin_tools.execute_tool(
            context,
            "add_role",
            {"member_id": "300", "role_id": "200"},
        )
        self.assertTrue(add_role.ok)
        self.assertEqual(guild.member.calls[-1], ("add_roles", 200, None))

        remove_role = await admin_tools.execute_tool(
            context,
            "remove_role",
            {"member_id": "300", "role_id": "200"},
        )
        self.assertTrue(remove_role.ok)
        self.assertEqual(guild.member.calls[-1], ("remove_roles", 200, None))

    async def test_channel_mutation_scope_preserved_by_resolved_type(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        context = admin_tools.AdminToolContext(guild=guild)

        text_rename = await admin_tools.execute_tool(
            context,
            "rename_channel",
            {"channel_id": "100", "name": "new-text"},
        )
        self.assertTrue(text_rename.ok)
        self.assertEqual(guild.text_channel.name, "new-text")

        voice_rename = await admin_tools.execute_tool(
            context,
            "rename_channel",
            {"channel_id": "101", "name": "new-voice"},
        )
        self.assertTrue(voice_rename.ok)
        self.assertEqual(guild.voice_channel.name, "new-voice")

        category_rename = await admin_tools.execute_tool(
            context,
            "rename_channel",
            {"channel_id": "90", "name": "bad-category"},
        )
        self.assertFalse(category_rename.ok)
        self.assertIn("text or voice", category_rename.message)

        voice_lock = await admin_tools.execute_tool(context, "lock_channel", {"channel_id": "101"})
        self.assertFalse(voice_lock.ok)
        self.assertIn("text channel", voice_lock.message)

        category_unlock = await admin_tools.execute_tool(context, "unlock_channel", {"channel_id": "90"})
        self.assertFalse(category_unlock.ok)
        self.assertIn("text channel", category_unlock.message)

        text_delete = await admin_tools.execute_tool(context, "delete_channel", {"channel_id": "100"})
        self.assertTrue(text_delete.ok)
        voice_delete = await admin_tools.execute_tool(context, "delete_channel", {"channel_id": "101"})
        self.assertTrue(voice_delete.ok)
        category_delete = await admin_tools.execute_tool(context, "delete_channel", {"channel_id": "90"})
        self.assertFalse(category_delete.ok)

        tricky_channel = FakeChannel(102, "stage-ish", channel_type="stage_voice")
        guild.channels.append(tricky_channel)
        tricky_rename = await admin_tools.execute_tool(
            context,
            "rename_channel",
            {"channel_id": "102", "name": "must-not-pass"},
        )
        self.assertFalse(tricky_rename.ok)
        self.assertEqual(tricky_channel.name, "stage-ish")


class AdminToolReadSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_guild_channels_roles_and_member_details_are_structured(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        context = admin_tools.AdminToolContext(guild=guild)

        summary = await admin_tools.execute_tool(context, "get_guild_summary", {})
        self.assertTrue(summary.ok)
        self.assertEqual(summary.data["id"], "10")
        self.assertEqual(summary.data["channel_count"], 3)
        self.assertEqual(summary.data["role_count"], 2)

        channel = await admin_tools.execute_tool(context, "get_channel_details", {"channel_id": "100"})
        self.assertTrue(channel.ok)
        self.assertEqual(channel.data["id"], "100")
        self.assertEqual(channel.data["parent_id"], "90")
        self.assertEqual(channel.data["permission_overwrites"][0]["target_id"], "200")

        roles = await admin_tools.execute_tool(context, "list_roles", {})
        self.assertEqual(roles.data["roles"][1]["id"], "200")
        self.assertEqual(roles.data["roles"][1]["permissions"], 4096)

        member = await admin_tools.execute_tool(context, "get_member_details", {"member_id": "300"})
        self.assertEqual(member.data["id"], "300")
        self.assertEqual(member.data["role_ids"], ["200"])
        self.assertEqual(member.data["guild_permissions"], 64)

    async def test_recent_messages_are_bounded_and_attachment_metadata_only(self):
        admin_tools = load_admin_tools()
        guild = FakeGuild()
        attachment = SimpleNamespace(id=700, filename="file.txt", size=12, content_type="text/plain")
        author = SimpleNamespace(id=800, display_name="Bob")
        message = SimpleNamespace(
            id=900,
            author=author,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content="content",
            attachments=[attachment],
        )
        guild.text_channel.history_messages = [message]
        context = admin_tools.AdminToolContext(guild=guild)

        result = await admin_tools.execute_tool(context, "get_recent_messages", {"channel_id": "100", "limit": 1})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["messages"][0]["id"], "900")
        self.assertEqual(result.data["messages"][0]["author_id"], "800")
        self.assertEqual(result.data["messages"][0]["attachments"][0]["id"], "700")
        self.assertEqual(result.data["messages"][0]["attachments"][0]["filename"], "file.txt")
        self.assertEqual(guild.text_channel.history_limit, 1)

        too_many = await admin_tools.execute_tool(
            context,
            "get_recent_messages",
            {"channel_id": "100", "limit": admin_tools.MAX_RECENT_MESSAGES + 1},
        )
        self.assertFalse(too_many.ok)
        zero = await admin_tools.execute_tool(context, "get_recent_messages", {"channel_id": "100", "limit": 0})
        self.assertFalse(zero.ok)


class AdminToolAccessTests(unittest.TestCase):
    def test_explicit_whitelist_helper_excludes_administrator_only(self):
        admin_tools = load_admin_tools()
        role = FakeRole(20, "Allowed")
        explicit_user = FakeMember(10, "User")
        explicit_role = FakeMember(11, "RoleUser", roles=[role])
        admin_only = FakeMember(12, "Admin", administrator=True)
        config = {
            "allow_server_administrators": True,
            "allowed_user_ids": [10],
            "allowed_role_ids": [20],
        }

        self.assertTrue(admin_tools.actor_is_explicitly_whitelisted(explicit_user, config))
        self.assertTrue(admin_tools.actor_is_explicitly_whitelisted(explicit_role, config))
        self.assertFalse(admin_tools.actor_is_explicitly_whitelisted(admin_only, config))
        self.assertTrue(admin_tools.actor_has_access(admin_only, config))


class AdminExecuteAdapterTests(unittest.TestCase):
    def test_execute_adapter_converts_discord_objects_to_json_safe_ids(self):
        sys.path.insert(0, str(CORE_ROOT))
        sys.modules.pop("Admin", None)
        sys.modules.pop("admin_tools", None)
        Admin = importlib.import_module("Admin")
        member = SimpleNamespace(id=300)
        role = SimpleNamespace(id=200)
        channel = SimpleNamespace(id=100)

        arguments = Admin.build_execute_tool_arguments(
            action_name="add_role",
            member=member,
            user_id=None,
            role=role,
            channel=None,
            voice_channel=None,
            name=None,
            reason="reason",
            count=None,
            duration_minutes=None,
            content=None,
        )
        self.assertEqual(arguments, {"reason": "reason", "member_id": "300", "role_id": "200"})

        message_arguments = Admin.build_execute_tool_arguments(
            action_name="send_message",
            member=None,
            user_id=None,
            role=None,
            channel=channel,
            voice_channel=None,
            name=None,
            reason="reason",
            count=None,
            duration_minutes=None,
            content="hello",
        )
        self.assertEqual(message_arguments, {"reason": "reason", "channel_id": "100", "content": "hello"})

    def test_execute_adapter_preserves_text_or_voice_single_channel_rule(self):
        sys.path.insert(0, str(CORE_ROOT))
        sys.modules.pop("Admin", None)
        Admin = importlib.import_module("Admin")
        channel = SimpleNamespace(id=100)
        voice_channel = SimpleNamespace(id=101)

        with self.assertRaisesRegex(ValueError, "requires only one"):
            Admin.build_execute_tool_arguments(
                action_name="rename_channel",
                member=None,
                user_id=None,
                role=None,
                channel=channel,
                voice_channel=voice_channel,
                name="new",
                reason="reason",
                count=None,
                duration_minutes=None,
                content=None,
            )

        arguments = Admin.build_execute_tool_arguments(
            action_name="delete_channel",
            member=None,
            user_id=None,
            role=None,
            channel=None,
            voice_channel=voice_channel,
            name=None,
            reason="reason",
            count=None,
            duration_minutes=None,
            content=None,
        )
        self.assertEqual(arguments, {"reason": "reason", "channel_id": "101"})


class AdminToolSecurityTests(unittest.TestCase):
    def test_no_system_or_arbitrary_code_surface_in_admin_tools(self):
        source = (CORE_ROOT / "admin_tools.py").read_text(encoding="utf-8")
        forbidden = ["subprocess", "shell=True", "os.system", "eval(", "exec(", "PowerShell", "cmd.exe"]
        for pattern in forbidden:
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source)


if __name__ == "__main__":
    unittest.main()
