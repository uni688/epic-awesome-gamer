import asyncio
from types import SimpleNamespace

import pytest

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


class FakeClickable:
    def __init__(self, visible: bool = True, enabled: bool = True):
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    @property
    def first(self):
        return self

    async def is_visible(self, timeout=None):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def click(self, force: bool = False):
        self.clicked = force


class FakeCheckoutPage:
    def __init__(self, selector_map: dict[str, FakeClickable] | None = None):
        self._selector_map = selector_map or {}

    def locator(self, selector: str):
        return self._selector_map.get(selector, FakeClickable(visible=False, enabled=False))


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


def test_click_cart_checkout_prefers_ranked_candidate():
    async def _runner():
        checkout_btn = FakeClickable()
        game = EpicGames(page=SimpleNamespace())

        async def _candidate(_page, _keywords):
            return {"locator": checkout_btn, "score": 60}

        game._find_best_button = _candidate
        await game._click_cart_checkout(FakeCheckoutPage())
        assert checkout_btn.clicked

    asyncio.run(_runner())


def test_click_cart_checkout_uses_fallback_selector():
    async def _runner():
        fallback_btn = FakeClickable()
        game = EpicGames(page=SimpleNamespace())

        async def _no_candidate(_page, _keywords):
            return None

        game._find_best_button = _no_candidate
        page = FakeCheckoutPage({"button:has-text('Check Out')": fallback_btn})
        await game._click_cart_checkout(page)
        assert fallback_btn.clicked

    asyncio.run(_runner())


def test_click_cart_checkout_raises_when_missing():
    async def _runner():
        game = EpicGames(page=SimpleNamespace())

        async def _no_candidate(_page, _keywords):
            return None

        game._find_best_button = _no_candidate
        with pytest.raises(TimeoutError):
            await game._click_cart_checkout(FakeCheckoutPage())

    asyncio.run(_runner())
