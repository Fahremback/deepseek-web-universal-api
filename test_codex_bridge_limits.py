import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).parent
PROBE="from harness_adapter import prepare_chat;prepare_chat({'model':'deepseek-web','messages':[{'role':'user','content':'x'*250000}]})"
class CodexBridgeLimitTests(unittest.TestCase):
    def probe(self, enabled):
        env=dict(os.environ)
        env.pop("DEEPSEEK_CODEX_BRIDGE",None)
        if enabled:
            env["DEEPSEEK_CODEX_BRIDGE"]="1"
        return subprocess.run([sys.executable,"-c",PROBE],cwd=ROOT,env=env,capture_output=True).returncode
    def test_regular_api_keeps_its_smaller_prompt_limit(self):
        self.assertNotEqual(self.probe(False),0)
    def test_codex_bridge_accepts_its_instruction_and_tool_catalog(self):
        self.assertEqual(self.probe(True),0)
if __name__=="__main__":
    unittest.main()
