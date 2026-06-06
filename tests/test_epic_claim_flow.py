import asyncio
from types import SimpleNamespace

from app.services.epic_games_service import EpicGames


class FakeLocator:
    def __init__(self, text: str = "", visible: bool = True):
        self._text = text
        self._visible = visible

    @property
    def first(self):
        return self

    async def text_content(self):
        return self._text

    async def is_visible(self, timeout=None):
        return self._visible


class FakePage:
    def __init__(self, text_map: dict[str, str] | None = None):
        self._text_map = text_map or {}

    def locator(self, selector: str):
        return FakeLocator(text=self._text_map.get(selector, ""), visible=True)


def test_classify_flow_signals():
    assert (
        EpicGames._classify_flow_signals(
            has_auth_gate=True, has_checkout_iframe=True, has_free_claim_action=True
        )
        == EpicGames.FLOW_AUTH_GATE
    )
    assert (
        EpicGames._classify_flow_signals(
            has_auth_gate=False, has_checkout_iframe=True, has_free_claim_action=True
        )
        == EpicGames.FLOW_CHECKOUT
    )
    assert (
        EpicGames._classify_flow_signals(
            has_auth_gate=False, has_checkout_iframe=False, has_free_claim_action=True
        )
        == EpicGames.FLOW_FREE_CLAIM
    )


def test_button_candidate_scoring():
    high = EpicGames._score_button_candidate(
        text="Add to Library",
        aria_label="Add to Library",
        visible=True,
        enabled=True,
        in_viewport=True,
        keywords=["add to library", "confirm"],
    )
    low = EpicGames._score_button_candidate(
        text="Wishlist",
        aria_label="",
        visible=False,
        enabled=False,
        in_viewport=False,
        keywords=["add to library", "confirm"],
    )
    assert high > low


def test_verify_claim_success_uses_order_history_fallback():
    async def _runner():
        page = FakePage(
            {
                "[role='alert']": "",
                "body": "",
            }
        )
        game = EpicGames(page=page, order_checker=lambda _: asyncio.sleep(0, result=True))

        async def _not_claimed(_page):
            return False

        game._is_claimed_on_product_page = _not_claimed

        promotion = SimpleNamespace(namespace="ns")
        assert await game._verify_claim_success(page, promotion) is True

    asyncio.run(_runner())


class FakeHiddenLocator(FakeLocator):
    async def is_visible(self, timeout=None):
        return False


class FakeNoChallengePage:
    def locator(self, selector: str):
        return FakeHiddenLocator(visible=False)

    async def wait_for_event(self, *args, **kwargs):
        raise TimeoutError("no matching event")


class FakeAgent:
    def __init__(self):
        self.wait_calls = 0
        self._captcha_payload = None
        self._captcha_payload_queue = asyncio.Queue()
        self._captcha_response_queue = asyncio.Queue()

    async def wait_for_challenge(self):
        self.wait_calls += 1
        return "success"


class FakeEventPage(FakeNoChallengePage):
    def __init__(self):
        self.handlers = {"request": [], "response": []}

    def on(self, event_name, handler):
        self.handlers[event_name].append(handler)

    def remove_listener(self, event_name, handler):
        self.handlers[event_name].remove(handler)


class FakeResponse:
    url = "https://store.epicgames.com/graphql"
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"errorCode": "errors.com.epicgames.common.captcha.hcaptcha_challenge"}

    async def text(self):
        return ""


def test_hcaptcha_challenge_payload_detection():
    assert EpicGames._contains_hcaptcha_challenge(
        {"errorCode": "errors.com.epicgames.common.captcha.hcaptcha_challenge"}
    )
    assert EpicGames._is_hcaptcha_challenge_url("https://newassets.hcaptcha.com/captcha/v1/api.js")
    assert not EpicGames._contains_hcaptcha_challenge({"status": "ok"})


def test_safe_wait_uses_challenge_signal_queue():
    async def _runner():
        agent = FakeAgent()
        signal = asyncio.Queue(maxsize=1)
        signal.put_nowait(True)

        await EpicGames._safe_wait_for_challenge(agent, FakeNoChallengePage(), signal)

        assert agent.wait_calls == 1

    asyncio.run(_runner())


def test_safe_wait_skips_when_no_challenge_signal():
    async def _runner():
        agent = FakeAgent()

        await EpicGames._safe_wait_for_challenge(agent, FakeNoChallengePage())

        assert agent.wait_calls == 0

    asyncio.run(_runner())


def test_hcaptcha_challenge_watcher_detects_response_body():
    async def _runner():
        page = FakeEventPage()
        signal, cleanup = EpicGames._watch_hcaptcha_challenge(page)

        for handler in page.handlers["response"]:
            handler(FakeResponse())
        await asyncio.sleep(0)

        assert not signal.empty()
        cleanup()
        assert page.handlers == {"request": [], "response": []}

    asyncio.run(_runner())
