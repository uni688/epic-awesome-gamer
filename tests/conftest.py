import os
import sys
import types

from pydantic import SecretStr
from pydantic_settings import BaseSettings

os.environ.setdefault("EPIC_EMAIL", "test@example.com")
os.environ.setdefault("EPIC_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")


class AgentConfig(BaseSettings):
    GEMINI_API_KEY: SecretStr | None = None
    EXECUTION_TIMEOUT: float = 1
    RESPONSE_TIMEOUT: float = 1
    RETRY_ON_FAILURE: bool = False
    ignore_request_questions: list[str] | None = None
    ignore_request_types: list[str] | None = None


class AgentV:
    def __init__(self, page=None, agent_config=None):
        self.page = page
        self.config = agent_config
        self.wait_calls = 0

    async def wait_for_challenge(self):
        self.wait_calls += 1
        return "success"


agent_module = types.ModuleType("hcaptcha_challenger.agent")
agent_module.AgentConfig = AgentConfig
agent_module.AgentV = AgentV

package_module = types.ModuleType("hcaptcha_challenger")
package_module.agent = agent_module
package_module.AgentConfig = AgentConfig
package_module.AgentV = AgentV

sys.modules.setdefault("hcaptcha_challenger", package_module)
sys.modules.setdefault("hcaptcha_challenger.agent", agent_module)
