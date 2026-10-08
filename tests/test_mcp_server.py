import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bambu_companion.bridge.studio_runner import StudioRunnerError
from bambu_companion.mcp_server import SUPPORTED_PROTOCOL_VERSIONS, TOOLS, McpServer, call_tool
from bambu_companion.mcp_server import main as mcp_main
from bambu_companion.profiles.project_3mf import read_project_settings
from bambu_companion.service import CompanionService

from .mesh_fixtures import cube_soup, write_stl_binary
from .project_fixtures import write_project_3mf

REPO_ROOT = Path(__file__).resolve().parent.parent


def _request(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def run_session(service: CompanionService, messages: list) -> dict:
    """Feeds `messages` to an in-process server and returns its replies
    keyed by request id."""
    raw = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages).encode("utf-8")
    out = io.BytesIO()
    McpServer(service, io.BytesIO(raw), out).serve_forever()
    replies = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
    return {r.get("id"): r for r in replies}


class McpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"APPDATA": str(self.dir / "AppData")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = CompanionService()
        self.addCleanup(self.service.close)
        self.project = write_project_3mf(self.dir / "box.3mf")
        self.stl = self.dir / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl)


class TestProtocol(McpCase):
    def test_initialize_echoes_a_supported_version_and_advertises_tools(self):
        replies = run_session(
            self.service,
            [_request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})],
        )
        result = replies[1]["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["serverInfo"]["name"], "bambu-companion")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("WAIT FOR APPROVAL", result["instructions"])

    def test_unknown_protocol_version_gets_our_latest(self):
        replies = run_session(self.service, [_request(1, "initialize", {"protocolVersion": "1999-01-01"})])
        self.assertEqual(replies[1]["result"]["protocolVersion"], SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_reply_and_ping_does(self):
        replies = run_session(
            self.service,
            [{"jsonrpc": "2.0", "method": "notifications/initialized"}, _request(7, "ping")],
        )
        self.assertEqual(list(replies), [7])
        self.assertEqual(replies[7]["result"], {})

    def test_unknown_method_and_garbage_are_protocol_errors_not_crashes(self):
        replies = run_session(self.service, ["this is not json", _request(2, "does/not/exist"), _request(3, "ping")])
        self.assertEqual(replies[None]["error"]["code"], -32700)
        self.assertEqual(replies[2]["error"]["code"], -32601)
        self.assertEqual(replies[3]["result"], {})

    def test_every_line_written_is_one_complete_json_rpc_message(self):
        raw = (json.dumps(_request(1, "tools/list")) + "\n").encode()
        out = io.BytesIO()
        McpServer(self.service, io.BytesIO(raw), out).serve_forever()
        lines = out.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["jsonrpc"], "2.0")


class TestToolList(McpCase):
    def test_tools_list_schemas_are_well_formed(self):
        tools = run_session(self.service, [_request(1, "tools/list")])[1]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [t["name"] for t in TOOLS])
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertTrue(set(schema["required"]) <= set(schema["properties"]), tool["name"])
            self.assertTrue(tool["description"])

    def test_no_tool_can_start_a_print_or_take_printer_credentials(self):
        names = {t["name"] for t in TOOLS}
        for forbidden in ("start_print", "send_gcode", "print", "pause_print", "stop_print", "set_temperature"):
            self.assertNotIn(forbidden, names)
        for tool in TOOLS:
            for prop in tool["inputSchema"]["properties"]:
                self.assertNotIn("access_code", prop)
                self.assertNotIn("password", prop)
                self.assertNotIn("serial", prop)
                self.assertNotIn("gcode", prop)

    def test_every_listed_tool_is_dispatchable(self):
        # A listed tool with no dispatch branch would raise KeyError,
        # which call_tool reports as an internal error. Bambu Studio is
        # made "not installed" so that running this suite on a PC that
        # has it never launches a real slice.
        not_installed = mock.patch(
            "bambu_companion.bridge.studio_runner.find_bambu_studio_executable",
            side_effect=StudioRunnerError("Bambu Studio is not installed (test)"),
        )
        with not_installed:
            for tool in TOOLS:
                result = call_tool(self.service, tool["name"], {})
                self.assertNotIn("Internal error", result["content"][0]["text"], tool["name"])


class TestToolCalls(McpCase):
    def call(self, name, **arguments):
        replies = run_session(self.service, [_request(1, "tools/call", {"name": name, "arguments": arguments})])
        result = replies[1]["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text))

    def test_analyze_model(self):
        is_error, data = self.call("analyze_model", model_path=str(self.stl))
        self.assertFalse(is_error)
        self.assertEqual(data["size_mm"], [20.0, 20.0, 20.0])

    def test_recommend_then_apply_round_trip(self):
        is_error, rec = self.call("recommend_settings", model_path=str(self.project), goal="strength")
        self.assertFalse(is_error)
        self.assertIn("wall_loops", [c["key"] for c in rec["changes"]])
        is_error, applied = self.call("apply_settings", job_id=rec["job_id"], approved_keys=["wall_loops"])
        self.assertFalse(is_error, applied)
        self.assertEqual(read_project_settings(applied["written"])["wall_loops"], "4")

    def test_user_errors_come_back_as_readable_tool_errors(self):
        is_error, text = self.call("analyze_model", model_path=str(self.dir / "missing.stl"))
        self.assertTrue(is_error)
        self.assertIn("File not found", text)
        is_error, text = self.call("apply_settings", source_project=str(self.project), changes={"machine_start_gcode": "M109"})
        self.assertTrue(is_error)
        self.assertIn("not a setting Bambu Companion is allowed to change", text)

    def test_unknown_tool(self):
        is_error, text = self.call("start_print")
        self.assertTrue(is_error)
        self.assertIn("Unknown tool", text)

    def test_a_bug_inside_a_tool_is_contained(self):
        with mock.patch.object(CompanionService, "studio_info", side_effect=RuntimeError("boom")):
            with mock.patch("sys.stderr", new=io.StringIO()):
                result = call_tool(self.service, "studio_info", {})
        self.assertTrue(result["isError"])
        self.assertIn("Internal error in studio_info: RuntimeError: boom", result["content"][0]["text"])


class TestRealProcess(unittest.TestCase):
    """Starts the launcher the way Claude Desktop does and speaks the
    protocol over real pipes — catches anything that would corrupt
    stdout (a stray print, a bad import) that in-process tests can't."""

    def test_launcher_speaks_clean_json_rpc_over_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            stl = Path(tmp) / "cube.stl"
            write_stl_binary(cube_soup(20.0), stl)
            messages = [
                _request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                _request(2, "tools/list"),
                _request(3, "tools/call", {"name": "analyze_model", "arguments": {"model_path": str(stl)}}),
            ]
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "launch_mcp.py")],
                input="".join(json.dumps(m) + "\n" for m in messages).encode("utf-8"),
                capture_output=True,
                timeout=120,
                cwd=tmp,  # not the repo: the launcher must find the package itself
                env={**os.environ, "APPDATA": tmp},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        replies = {}
        for line in proc.stdout.decode("utf-8").splitlines():
            message = json.loads(line)  # every stdout line must be JSON
            replies[message["id"]] = message
        self.assertEqual(set(replies), {1, 2, 3})
        self.assertEqual(len(replies[2]["result"]["tools"]), len(TOOLS))
        analysis = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(analysis["size_mm"], [20.0, 20.0, 20.0])



class TestRobustness(McpCase):
    def test_tool_name_of_any_json_type_is_answered(self):
        messages = [
            _request(i, "tools/call", {"name": bad, "arguments": {}})
            for i, bad in enumerate([["analyze_model"], {"x": 1}, 5, None, True], start=1)
        ]
        replies = run_session(self.service, messages)
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5])
        for reply in replies.values():
            self.assertTrue(reply["result"]["isError"])
            self.assertIn("Unknown tool", reply["result"]["content"][0]["text"])

    def test_request_ids_of_zero_and_string_are_echoed(self):
        replies = run_session(self.service, [_request(0, "ping"), _request("abc", "ping")])
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual(replies["abc"]["result"], {})

    def test_arguments_of_the_wrong_json_type_do_not_crash_the_call(self):
        replies = run_session(
            self.service,
            [
                _request(1, "tools/call", {"name": "analyze_model", "arguments": ["not", "an", "object"]}),
                _request(2, "tools/call", {"name": "slice_model", "arguments": {"model_path": str(self.project), "wait_s": "40s"}}),
                _request(3, "tools/call"),
            ],
        )
        for request_id in (1, 2, 3):
            self.assertTrue(replies[request_id]["result"]["isError"], request_id)
            self.assertNotIn("Internal error", replies[request_id]["result"]["content"][0]["text"])
        self.assertIn("wait_s must be a number", replies[2]["result"]["content"][0]["text"])

    def test_exception_escaping_call_tool_still_produces_one_reply(self):
        with mock.patch("bambu_companion.mcp_server.call_tool", side_effect=RuntimeError("boom")):
            with mock.patch("sys.stderr", new=io.StringIO()):
                replies = run_session(self.service, [_request(1, "tools/call", {"name": "studio_info"})])
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertIn("RuntimeError: boom", replies[1]["result"]["content"][0]["text"])

    def test_multi_plate_argument_reaches_the_analyzer(self):
        from .project_fixtures import write_two_plate_project

        two = write_two_plate_project(self.dir / "two.3mf")
        replies = run_session(
            self.service,
            [_request(1, "tools/call", {"name": "analyze_model", "arguments": {"model_path": str(two), "plate": 2}})],
        )
        data = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(data["size_mm"], [10.0, 10.0, 10.0])


class TestStdoutCarriesOnlyTheProtocol(McpCase):
    def test_a_tool_that_prints_does_not_corrupt_the_protocol_stream(self):
        # Any library warning or stray print() on stdout would be read
        # by the host as a (broken) protocol message.
        class Chatty(CompanionService):
            def studio_info(self):
                print("stray debug line on stdout")
                return {"ok": True}

        request = json.dumps(_request(1, "tools/call", {"name": "studio_info", "arguments": {}})) + "\n"
        fake_in = io.TextIOWrapper(io.BytesIO(request.encode("utf-8")), encoding="utf-8")
        out_bytes = io.BytesIO()
        fake_out = io.TextIOWrapper(out_bytes, encoding="utf-8")
        stray = io.StringIO()
        real_stdout = sys.stdout
        try:
            with mock.patch("sys.stdin", fake_in), mock.patch("sys.stderr", stray):
                sys.stdout = fake_out
                self.assertEqual(mcp_main(Chatty), 0)
        finally:
            sys.stdout = real_stdout
        lines = out_bytes.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], 1)
        self.assertIn("stray debug line", stray.getvalue())


if __name__ == "__main__":
    unittest.main()
