# -*- coding: utf-8 -*-
# Time       : 2022/1/16 0:25
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description: 游戏商城控制句柄（增强稳定版）

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional
from urllib.parse import urlparse

import httpx
from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import (
    FrameLocator,
    Locator,
    Page,
    TimeoutError,
    expect,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from models import Order, OrderItem, PromotionGame
from settings import RUNTIME_DIR, settings

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"
URL_LOGIN = (
    f"https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true&redirectUrl={URL_CLAIM}"
)
URL_CART = "https://store.epicgames.com/en-US/cart"
URL_CART_SUCCESS = "https://store.epicgames.com/en-US/cart/success"
URL_PROMOTIONS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
URL_PRODUCT_PAGE = "https://store.epicgames.com/en-US/p/"
URL_PRODUCT_BUNDLES = "https://store.epicgames.com/en-US/bundles/"

DEBUG_DIR = RUNTIME_DIR.joinpath("epic_debug")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _safe_upper(text: str | None) -> str:
    return (text or "").upper()


def _clean_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def get_promotions() -> List[PromotionGame]:
    """获取周免游戏数据。"""

    def is_discount_game(prot: dict) -> bool | None:
        with suppress(KeyError, IndexError, TypeError):
            offers = prot["promotions"]["promotionalOffers"][0]["promotionalOffers"]
            for offer in offers:
                if offer["discountSetting"]["discountPercentage"] == 0:
                    return True
        return False

    promotions: List[PromotionGame] = []

    try:
        resp = httpx.get(URL_PROMOTIONS, params={"local": "zh-CN"}, timeout=30)
        resp.raise_for_status()
    except Exception as err:
        logger.exception(f"获取周免数据失败: {err}")
        return []

    try:
        data = resp.json()
    except JSONDecodeError as err:
        logger.error("Failed to decode promotions json", err=err)
        return []

    with suppress(Exception):
        cache_key = RUNTIME_DIR.joinpath("promotions.json")
        cache_key.parent.mkdir(parents=True, exist_ok=True)
        cache_key.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    for e in data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", []):
        if not is_discount_game(e):
            continue

        is_bundle = False
        if e.get("offerType") == "BUNDLE":
            is_bundle = True
        if not is_bundle:
            for cat in e.get("categories", []):
                if "bundle" in _safe_upper(cat.get("path")):
                    is_bundle = True
                    break
        if not is_bundle and "Collection" in (e.get("title") or ""):
            is_bundle = True

        base_url = URL_PRODUCT_BUNDLES if is_bundle else URL_PRODUCT_PAGE

        try:
            if e.get("offerMappings"):
                slug = e["offerMappings"][0]["pageSlug"]
                e["url"] = f"{base_url.rstrip('/')}/{slug}"
            elif e.get("productSlug"):
                e["url"] = f"{base_url.rstrip('/')}/{e['productSlug']}"
            else:
                e["url"] = f"{base_url.rstrip('/')}/{e.get('urlSlug', 'unknown')}"
        except (KeyError, IndexError, TypeError) as err:
            logger.warning(f"Failed to build url for promotion: {e.get('title')} err={err}")
            continue

        try:
            promotion = PromotionGame(**e)
        except Exception as err:
            logger.warning(f"Failed to parse promotion model: {e.get('title')} err={err}")
            continue

        logger.info(promotion.url)
        promotions.append(promotion)

    return promotions


class EpicAgent:
    def __init__(self, page: Page):
        self.page = page
        self._promotions: List[PromotionGame] = []
        self._ctx_cookies_is_available: bool = False
        self._orders: List[OrderItem] = []
        self._namespaces: List[str] = []
        self._cookies = None
        self.epic_games = EpicGames(self.page, self.is_namespace_claimed)

    async def _sync_order_history(self, force: bool = False):
        if self._orders and not force:
            return

        completed_orders: List[OrderItem] = []
        try:
            await self.page.goto("https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory")
            text_content = await self.page.text_content("//pre")
            if not text_content:
                logger.warning("Order history response is empty")
                self._orders = []
                return

            data = json.loads(text_content)
            for _order in data.get("orders", []):
                order = Order(**_order)
                if order.orderType != "PURCHASE":
                    continue
                for item in order.items:
                    if not item.namespace or len(item.namespace) != 32:
                        continue
                    completed_orders.append(item)
        except Exception as err:
            logger.warning(f"同步订单历史失败: {err}")
        self._orders = completed_orders

    async def _check_orders(self):
        await self._sync_order_history()
        self._namespaces = self._namespaces or [order.namespace for order in self._orders]
        self._promotions = [p for p in get_promotions() if p.namespace not in self._namespaces]

    async def is_namespace_claimed(self, namespace: str) -> bool:
        await self._sync_order_history(force=True)
        namespaces = [order.namespace for order in self._orders]
        return namespace in namespaces

    async def _should_ignore_task(self) -> bool:
        self._ctx_cookies_is_available = False
        try:
            await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
        except Exception as err:
            logger.error(f"打开免费游戏页面失败: {err}")
            return False

        with suppress(Exception):
            status = await self.page.locator("//egs-navigation").get_attribute("isloggedin")
            if status == "false":
                logger.error("❌ context cookies is not available")
                return False

        self._ctx_cookies_is_available = True
        await self._check_orders()
        return not self._promotions

    async def collect_epic_games(self):
        if await self._should_ignore_task():
            logger.success("All week-free games are already in the library")
            return

        if not self._ctx_cookies_is_available:
            logger.error("上下文 cookie 不可用，任务终止")
            return

        if not self._promotions:
            await self._check_orders()

        if not self._promotions:
            logger.success("All week-free games are already in the library")
            return

        for p in self._promotions:
            logger.debug(
                "Discover promotion \n"
                + json.dumps({"title": p.title, "url": p.url}, indent=2, ensure_ascii=False)
            )

        try:
            await self.epic_games.collect_weekly_games(self._promotions)
        except Exception as e:
            logger.exception(e)

        logger.debug("All tasks in the workflow have been completed")


class EpicGames:
    FLOW_FREE_CLAIM = "FREE_CLAIM_FLOW"
    FLOW_CHECKOUT = "CHECKOUT_FLOW"
    FLOW_AUTH_GATE = "AUTH_GATE_FLOW"
    FLOW_UNKNOWN = "UNKNOWN_FLOW"

    IFRAME_SELECTOR = (
        "//iframe[contains(@id, 'webPurchaseContainer') "
        "or contains(@src, 'purchase') "
        "or contains(@src, 'checkout')]"
    )

    def __init__(
        self,
        page: Page,
        order_checker: Callable[[str], Awaitable[bool]] | None = None,
    ):
        self.page = page
        self._order_checker = order_checker
        self._game_results: List[dict] = []
        self._last_debug_dump: Optional[Path] = None

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @staticmethod
    def _page_is_closed(page: Page) -> bool:
        with suppress(Exception):
            return page.is_closed()
        return False

    def _ensure_page_alive(self, stage: str):
        if self._page_is_closed(self.page):
            logger.warning(f"页面已关闭，阶段[{stage}]将尝试恢复")

    async def _recover_page_if_closed(self, page: Page, stage: str) -> Page:
        """当页面意外关闭时，基于同一 context 尝试重建页面。"""
        if not self._page_is_closed(page):
            return page

        logger.warning(f"检测到页面已关闭，准备恢复: stage={stage}")
        with suppress(Exception):
            context = page.context
            new_page = await context.new_page()
            self.page = new_page
            self.epic_games.page = new_page
            logger.success(f"页面已恢复: stage={stage}")
            return new_page

        logger.error(f"页面恢复失败: stage={stage}")
        return page

    async def _dump_debug_snapshot(
        self, page: Page, tag: str, promotion: PromotionGame | None = None
    ):
        """尽可能保留现场信息，便于后续定位按钮或页面状态。"""
        try:
            ts = _now_tag()
            safe_tag = re.sub(r"[^a-zA-Z0-9_\-]+", "_", tag)[:80]
            base = DEBUG_DIR / f"{ts}_{safe_tag}"
            base.parent.mkdir(parents=True, exist_ok=True)

            with suppress(Exception):
                await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)

            with suppress(Exception):
                html = await page.content()
                base.with_suffix(".html").write_text(html, encoding="utf-8")

            body_text = ""
            with suppress(Exception):
                body_text = _clean_text(await page.locator("body").text_content())

            info = {
                "tag": tag,
                "url": None,
                "title": None,
                "promotion": None,
                "body_text_sample": body_text[:2000],
                "frames": [],
                "buttons": [],
                "time": datetime.now().isoformat(),
            }

            with suppress(Exception):
                info["url"] = page.url
            with suppress(Exception):
                info["title"] = await page.title()
            if promotion is not None:
                info["promotion"] = {"title": promotion.title, "url": promotion.url}

            with suppress(Exception):
                info["frames"] = [{"url": fr.url, "name": fr.name} for fr in page.frames[:20]]

            candidates = await self._collect_visible_button_snapshot(page)
            info["buttons"] = candidates[:40]

            base.with_suffix(".json").write_text(
                json.dumps(info, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._last_debug_dump = base
            logger.debug(f"调试快照已保存: {base}")
        except Exception as err:
            logger.debug(f"保存调试快照失败: {err}")

    async def _collect_visible_button_snapshot(self, page: Page) -> List[dict]:
        selectors = [
            "button",
            "[role='button']",
            "a[role='button']",
            "[data-testid*='cta']",
            "[data-testid*='purchase']",
        ]
        out: List[dict] = []
        seen: set[str] = set()

        for selector in selectors:
            with suppress(Exception):
                locator = page.locator(selector)
                count = min(await locator.count(), 40)
                for i in range(count):
                    item = locator.nth(i)
                    key = f"{selector}:{i}"
                    if key in seen:
                        continue
                    seen.add(key)

                    try:
                        visible = await item.is_visible(timeout=1500)
                    except Exception:
                        visible = False
                    if not visible:
                        continue

                    text = _clean_text(await item.text_content())
                    aria = _clean_text(await item.get_attribute("aria-label"))
                    data_testid = _clean_text(await item.get_attribute("data-testid"))
                    role = _clean_text(await item.get_attribute("role"))
                    out.append(
                        {
                            "selector": selector,
                            "index": i,
                            "text": text[:120],
                            "aria": aria[:120],
                            "data-testid": data_testid[:120],
                            "role": role[:40],
                            "visible": True,
                        }
                    )
        return out

    @staticmethod
    def _has_negative_button_signal(text: str = "", aria_label: str = "") -> bool:
        merged = _safe_upper(f"{text} {aria_label}")
        negative_flags = [
            "COMING SOON",
            "UNAVAILABLE",
            "ADD TO WISHLIST",
            "WISHLIST",
            "NOTIFY ME",
            "FREE JUN",
            "FREE JUL",
            "FREE AUG",
            "FREE SEP",
            "FREE OCT",
            "FREE NOV",
            "FREE DEC",
            "FREE JAN",
            "FREE FEB",
            "FREE MAR",
            "FREE APR",
            "FREE MAY",
        ]
        return any(flag in merged for flag in negative_flags)

    @staticmethod
    def _score_button_candidate(
        text: str = "",
        aria_label: str = "",
        visible: bool = False,
        enabled: bool = False,
        in_viewport: bool = False,
        keywords: List[str] | None = None,
    ) -> int:
        score = 0
        text_upper = _safe_upper(text)
        aria_upper = _safe_upper(aria_label)

        if keywords and any(word.upper() in text_upper for word in keywords):
            score += 50
        if keywords and any(word.upper() in aria_upper for word in keywords):
            score += 25
        if EpicGames._has_negative_button_signal(text, aria_label):
            score -= 100
        if visible:
            score += 20
        if enabled:
            score += 15
        if in_viewport:
            score += 10
        return score

    @staticmethod
    def _classify_flow_signals(
        has_auth_gate: bool,
        has_checkout_iframe: bool,
        has_free_claim_action: bool,
    ) -> str:
        if has_auth_gate:
            return EpicGames.FLOW_AUTH_GATE
        if has_checkout_iframe:
            return EpicGames.FLOW_CHECKOUT
        if has_free_claim_action:
            return EpicGames.FLOW_FREE_CLAIM
        return EpicGames.FLOW_UNKNOWN

    @staticmethod
    def _has_success_text(text: str) -> bool:
        text_upper = _safe_upper(text)
        success_flags = [
            "IN LIBRARY",
            "OWNED",
            "THANK YOU",
            "ORDER COMPLETE",
            "ORDER COMPLETED",
            "PURCHASE SUCCESSFUL",
            "PURCHASE COMPLETE",
            "ALREADY OWNED",
            "ADDED TO YOUR LIBRARY",
            "已在库中",
            "已拥有",
            "领取成功",
            "订单完成",
            "购买成功",
        ]
        return any(flag.upper() in text_upper for flag in success_flags)

    @staticmethod
    def _is_clickable_text(text: str, aria: str) -> bool:
        merged = _safe_upper(f"{text} {aria}")
        patterns = [
            "GET",
            "获取",
            "领取",
            "FREE",
            "免费",
            "ADD TO LIBRARY",
            "IN LIBRARY",
            "OWNED",
            "BUY NOW",
            "PLACE ORDER",
            "CHECK OUT",
            "CHECKOUT",
            "CONTINUE",
            "CONFIRM",
            "CONFIRM ORDER",
            "SUBMIT ORDER",
            "ORDER NOW",
        ]
        return any(p.upper() in merged for p in patterns)

    @staticmethod
    def _compose_candidate_keywords() -> List[str]:
        return [
            "get",
            "获取",
            "领取",
            "free",
            "免费",
            "add to library",
            "in library",
            "owned",
            "buy now",
            "place order",
            "check out",
            "checkout",
            "continue",
            "confirm",
            "confirm order",
            "submit order",
            "order now",
            "加入库",
        ]

    HCAPTCHA_CHALLENGE_KEYWORDS = (
        "hcaptcha_challenge",
        "hcaptcha",
        "/getcaptcha/",
        "/checkcaptcha/",
    )

    @classmethod
    def _contains_hcaptcha_challenge(cls, payload: Any) -> bool:
        if payload is None:
            return False

        if isinstance(payload, str):
            return any(keyword in payload.lower() for keyword in cls.HCAPTCHA_CHALLENGE_KEYWORDS)

        if isinstance(payload, dict):
            return any(
                cls._contains_hcaptcha_challenge(key) or cls._contains_hcaptcha_challenge(value)
                for key, value in payload.items()
            )

        if isinstance(payload, (list, tuple, set)):
            return any(cls._contains_hcaptcha_challenge(item) for item in payload)

        return False

    @classmethod
    def _is_hcaptcha_challenge_url(cls, url: str | None) -> bool:
        return cls._contains_hcaptcha_challenge(url or "")

    @staticmethod
    def _agent_has_pending_hcaptcha_challenge(agent: AgentV) -> bool:
        for attr in ("_captcha_payload",):
            with suppress(Exception):
                if getattr(agent, attr) is not None:
                    return True

        for attr in ("_captcha_payload_queue", "_captcha_response_queue"):
            with suppress(Exception):
                queue = getattr(agent, attr)
                if not queue.empty():
                    return True

        return False

    @classmethod
    async def _wait_for_hcaptcha_challenge_signal(cls, page: Page, timeout_ms: int = 5000) -> bool:
        async def _wait_for_event(event_name: str) -> bool:
            event = await page.wait_for_event(
                event_name,
                predicate=lambda item: cls._is_hcaptcha_challenge_url(getattr(item, "url", "")),
                timeout=timeout_ms,
            )
            return event is not None

        tasks = [
            asyncio.create_task(_wait_for_event("request")),
            asyncio.create_task(_wait_for_event("response")),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            detected = False
            for task in done:
                if task.cancelled():
                    continue
                with suppress(Exception):
                    detected = detected or bool(task.result())
            return detected
        except Exception:
            return False
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    def _watch_hcaptcha_challenge(cls, page: Page):
        challenge_signal: asyncio.Queue[bool] = asyncio.Queue(maxsize=1)
        created_tasks: set[asyncio.Task] = set()

        def _signal_challenge():
            if challenge_signal.empty():
                challenge_signal.put_nowait(True)

        def _handle_request(request):
            if cls._is_hcaptcha_challenge_url(getattr(request, "url", "")):
                _signal_challenge()

        async def _inspect_response(response):
            if cls._is_hcaptcha_challenge_url(getattr(response, "url", "")):
                _signal_challenge()
                return

            with suppress(Exception):
                headers = getattr(response, "headers", {}) or {}
                content_type = headers.get("content-type", "")
                if "json" in content_type.lower():
                    if cls._contains_hcaptcha_challenge(await response.json()):
                        _signal_challenge()
                        return

            with suppress(Exception):
                text = await response.text()
                if cls._contains_hcaptcha_challenge(text):
                    _signal_challenge()

        def _handle_response(response):
            task = asyncio.create_task(_inspect_response(response))
            created_tasks.add(task)
            task.add_done_callback(created_tasks.discard)

        with suppress(Exception):
            page.on("request", _handle_request)
        with suppress(Exception):
            page.on("response", _handle_response)

        def _cleanup():
            for task in list(created_tasks):
                if not task.done():
                    task.cancel()
            with suppress(Exception):
                page.remove_listener("request", _handle_request)
            with suppress(Exception):
                page.remove_listener("response", _handle_response)

        return challenge_signal, _cleanup

    @staticmethod
    async def _safe_wait_for_challenge(
        agent: AgentV,
        page: Page,
        challenge_signal: asyncio.Queue[bool] | None = None,
    ):
        challenge_markers = [
            "iframe[src*='hcaptcha']",
            "[data-hcaptcha-response]",
            "text=/I am human|hCaptcha|Verify/i",
        ]
        has_challenge = EpicGames._agent_has_pending_hcaptcha_challenge(agent)

        if not has_challenge and challenge_signal is not None:
            with suppress(Exception):
                has_challenge = not challenge_signal.empty()

        if not has_challenge:
            for marker in challenge_markers:
                with suppress(Exception):
                    if await page.locator(marker).first.is_visible(timeout=1500):
                        has_challenge = True
                        break

        if not has_challenge:
            has_challenge = await EpicGames._wait_for_hcaptcha_challenge_signal(page)

        if not has_challenge:
            return

        try:
            logger.debug("检测到 hcaptcha_challenge，调用内置挑战处理接口")
            await agent.wait_for_challenge()
        except Exception as err:
            logger.warning(f"Captcha handler isolated error: {err}")

    @staticmethod
    async def _is_cloudflare_challenge_page(page: Page) -> bool:
        with suppress(Exception):
            title = _safe_upper(await page.title())
            if "JUST A MOMENT" in title or "ATTENTION REQUIRED" in title:
                return True

        with suppress(Exception):
            raw_url = page.url
            url = _safe_upper(raw_url)
            host = (urlparse(raw_url).hostname or "").lower()
            if "__CF_CHL" in url or host == "challenges.cloudflare.com":
                return True

        with suppress(Exception):
            body = _safe_upper(await page.locator("body").text_content())
            challenge_flags = [
                "ONE MORE STEP",
                "SECURITY CHECK",
                "ENABLE JAVASCRIPT AND COOKIES",
                "VERIFY YOU ARE HUMAN",
                "VERIFICATION SUCCESSFUL. WAITING",
            ]
            if any(flag in body for flag in challenge_flags):
                return True

        with suppress(Exception):
            return any(
                (urlparse(frame.url).hostname or "").lower() == "challenges.cloudflare.com"
                for frame in page.frames
            )

        return False

    async def _wait_for_cloudflare_clearance(
        self, page: Page, promotion: PromotionGame | None = None, timeout_ms: int = 45000
    ) -> bool:
        if not await self._is_cloudflare_challenge_page(page):
            return True

        logger.warning("Cloudflare challenge detected; waiting for clearance before continuing")
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            await page.wait_for_timeout(2500)
            if not await self._is_cloudflare_challenge_page(page):
                logger.success("Cloudflare challenge cleared")
                return True

            if promotion is not None:
                with suppress(Exception):
                    if promotion.url.rstrip("/").split("/")[-1] not in page.url:
                        await page.goto(promotion.url, wait_until="domcontentloaded", timeout=30000)

        await self._dump_debug_snapshot(page, "cloudflare_challenge_timeout", promotion)
        logger.warning("Cloudflare challenge did not clear within timeout")
        return False

    @staticmethod
    async def _agree_license(page: Page):
        logger.debug("Agree license")
        with suppress(Exception):
            check = page.locator("//label[@for='agree']")
            if await check.is_visible(timeout=4000):
                await check.click()

        with suppress(Exception):
            accept = page.locator("//button//span[normalize-space()='Accept']")
            if await accept.is_visible(timeout=3000) and await accept.is_enabled():
                await accept.click()

        with suppress(Exception):
            accept = page.locator(
                "//button[contains(., 'Accept') or contains(., '同意') or contains(., '接受')]"
            )
            if await accept.first.is_visible(timeout=2000):
                await accept.first.click(force=True)

    @staticmethod
    async def _safe_reload(page: Page):
        try:
            logger.debug("Attempting to reload current page...")
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"Reload failed ({e}), attempting to navigate to free games list...")
            with suppress(Exception):
                await page.goto(URL_CLAIM, wait_until="domcontentloaded", timeout=45000)

    @staticmethod
    async def _uk_confirm_order(wpc: FrameLocator):
        logger.debug("UK confirm order")
        with suppress(Exception):
            accept = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")
            if await accept.is_visible(timeout=5000) and await accept.is_enabled():
                await accept.click()
                return True

        with suppress(Exception):
            accept = wpc.locator(
                "button",
                has_text=re.compile(
                    r"(confirm|place order|submit order|complete order|pay now)", re.I
                ),
            )
            if await accept.first.is_visible(timeout=5000):
                await accept.first.click(force=True)
                return True
        return False

    async def _handle_content_gate(self, page: Page):
        """处理内容警告和年龄验证。"""
        logger.debug("Checking for content gates...")

        with suppress(Exception):
            age_select = page.locator("select#age-gate-year")
            if await age_select.is_visible(timeout=2000):
                await age_select.select_option(value="1990")
                await page.click("//button[contains(., 'Continue') or contains(., '继续')]")
                logger.success("Age gate passed")

        content_warning_selectors = [
            "//button//span[contains(text(), 'Continue')]",
            "//button//span[contains(text(), 'Agree')]",
            "//button[contains(@class, 'confirm')]",
            "[data-testid='warning-button-continue']",
            "//button[contains(., 'Continue')]",
            "//button[contains(., 'Accept')]",
            "//button[contains(., '继续')]",
            "//button[contains(., '同意')]",
            "//button[contains(., '接受')]",
        ]
        for selector in content_warning_selectors:
            with suppress(Exception):
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=1500):
                    await btn.click(force=True)
                    logger.success(f"Content gate passed: {selector}")
                    await page.wait_for_timeout(800)

    async def _is_auth_gate_present(self, page: Page) -> bool:
        auth_selectors = [
            "#email",
            "#password",
            "#sign-in",
            "text=/Sign in|Log in|登录|邮箱|密码/i",
        ]
        for selector in auth_selectors:
            with suppress(Exception):
                if await page.locator(selector).first.is_visible(timeout=1200):
                    return True
        return False

    async def _has_checkout_iframe(self, page: Page) -> bool:
        with suppress(Exception):
            iframe = page.locator(self.IFRAME_SELECTOR)
            if await iframe.count() > 0:
                return True

        with suppress(Exception):
            return any("purchase" in frame.url or "checkout" in frame.url for frame in page.frames)

        return False

    async def _classify_flow(self, page: Page) -> str:
        has_auth_gate = await self._is_auth_gate_present(page)
        has_checkout_iframe = await self._has_checkout_iframe(page)
        free_candidate = await self._find_best_button(page, self._compose_candidate_keywords())
        has_free_claim_action = bool(free_candidate and free_candidate.get("score", 0) > 0)
        return self._classify_flow_signals(
            has_auth_gate=has_auth_gate,
            has_checkout_iframe=has_checkout_iframe,
            has_free_claim_action=has_free_claim_action,
        )

    async def _collect_button_candidates(
        self, scope, keywords: List[str], source: str
    ) -> List[dict]:
        selectors = [
            "button",
            "[role='button']",
            "a[role='button']",
            "[data-testid*='purchase']",
            "[data-testid*='cta']",
        ]
        candidates: List[dict] = []

        for selector in selectors:
            try:
                locator = scope.locator(selector)
                count = min(await locator.count(), 40)
            except Exception:
                continue

            for i in range(count):
                try:
                    btn = locator.nth(i)
                    text = _clean_text(await btn.text_content())
                    aria = _clean_text(await btn.get_attribute("aria-label"))
                    data_testid = _clean_text(await btn.get_attribute("data-testid"))
                    if not text and not aria and not data_testid:
                        continue

                    visible = False
                    enabled = False
                    in_viewport = False
                    with suppress(Exception):
                        visible = await btn.is_visible()
                    with suppress(Exception):
                        enabled = await btn.is_enabled()
                    with suppress(Exception):
                        box = await btn.bounding_box()
                        in_viewport = bool(
                            box and box.get("width", 0) > 0 and box.get("height", 0) > 0
                        )

                    if self._has_negative_button_signal(text, aria or data_testid):
                        continue

                    score = self._score_button_candidate(
                        text=text,
                        aria_label=aria or data_testid,
                        visible=visible,
                        enabled=enabled,
                        in_viewport=in_viewport,
                        keywords=keywords,
                    )
                    if self._is_clickable_text(text, aria or data_testid):
                        score += 15

                    candidates.append(
                        {
                            "locator": btn,
                            "text": text,
                            "aria": aria,
                            "data-testid": data_testid,
                            "score": score,
                            "visible": visible,
                            "enabled": enabled,
                            "source": source,
                            "selector": selector,
                        }
                    )
                except Exception:
                    continue

        return candidates

    async def _find_best_button(self, page: Page, keywords: List[str]) -> dict | None:
        self._ensure_page_alive("find_best_button")
        candidates: List[dict] = []

        try:
            candidates.extend(await self._collect_button_candidates(page, keywords, "main_dom"))
        except Exception as err:
            logger.debug(f"主文档按钮收集失败: {err}")

        try:
            await page.locator(self.IFRAME_SELECTOR).first.wait_for(state="visible", timeout=2000)
            iframe = page.frame_locator(self.IFRAME_SELECTOR).first
            candidates.extend(await self._collect_button_candidates(iframe, keywords, "iframe"))
        except Exception:
            pass

        try:
            shadow_candidate = await page.evaluate(
                """
                (words) => {
                  const queue = [document];
                  const out = [];
                  while (queue.length) {
                    const root = queue.shift();
                    const elements = root.querySelectorAll("button,[role='button'],[aria-label],a[role='button']");
                    for (const el of elements) {
                      const text = (el.textContent || "").trim();
                      const aria = (el.getAttribute("aria-label") || "").trim();
                      const dataTestid = (el.getAttribute("data-testid") || "").trim();
                      if (!text && !aria && !dataTestid) continue;
                      const full = `${text} ${aria} ${dataTestid}`.toUpperCase();
                      const negative = ["COMING SOON", "WISHLIST", "NOTIFY ME", "UNAVAILABLE"];
                      if (negative.some((w) => full.includes(w))) continue;
                      if (words.some((w) => full.includes(w.toUpperCase()))) {
                        out.push({
                          text,
                          aria,
                          data_testid: dataTestid,
                          tag: el.tagName.toLowerCase(),
                        });
                        return out[0];
                      }
                    }
                    const hosts = root.querySelectorAll("*");
                    for (const host of hosts) {
                      if (host.shadowRoot) queue.push(host.shadowRoot);
                    }
                  }
                  return null;
                }
                """,
                keywords,
            )
            if shadow_candidate:
                candidates.append(
                    {
                        "locator": None,
                        "text": shadow_candidate.get("text") or "",
                        "aria": shadow_candidate.get("aria") or "",
                        "data-testid": shadow_candidate.get("data_testid") or "",
                        "score": 70,
                        "visible": True,
                        "enabled": True,
                        "source": "shadow_dom",
                        "selector": shadow_candidate.get("tag") or "unknown",
                    }
                )
        except Exception as err:
            logger.debug(f"shadow dom 搜索失败: {err}")

        if not candidates:
            await page.wait_for_timeout(1200)
            try:
                candidates.extend(
                    await self._collect_button_candidates(page, keywords, "main_dom_wait")
                )
            except Exception:
                pass

        if not candidates:
            return None

        candidates.sort(
            key=lambda c: (
                c["score"],
                1 if c["source"] == "main_dom" else 0,
                1 if c["visible"] else 0,
                1 if c["enabled"] else 0,
            ),
            reverse=True,
        )

        top = candidates[:10]
        logger.debug(
            "按钮候选列表: "
            + json.dumps(
                [
                    {
                        "score": c["score"],
                        "source": c["source"],
                        "selector": c["selector"],
                        "text": c["text"][:80],
                        "aria": c["aria"][:80],
                        "data-testid": c.get("data-testid", "")[:80],
                        "visible": c["visible"],
                        "enabled": c["enabled"],
                    }
                    for c in top
                ],
                ensure_ascii=False,
            )
        )
        return candidates[0]

    async def _log_page_state(self, page: Page, stage: str, promotion: PromotionGame | None = None):
        try:
            payload = {
                "stage": stage,
                "url": None,
                "title": None,
                "closed": self._page_is_closed(page),
                "promotion": None,
            }
            with suppress(Exception):
                payload["url"] = page.url
            with suppress(Exception):
                payload["title"] = await page.title()
            if promotion is not None:
                payload["promotion"] = {"title": promotion.title, "url": promotion.url}

            with suppress(Exception):
                body = await page.locator("body").text_content()
                payload["body_sample"] = _clean_text(body)[:1200]

            with suppress(Exception):
                payload["frames"] = [{"url": fr.url, "name": fr.name} for fr in page.frames[:20]]

            logger.debug(f"页面状态[{stage}]: {json.dumps(payload, ensure_ascii=False)}")
        except Exception as err:
            logger.debug(f"记录页面状态失败[{stage}]: {err}")

    async def _click_locator_with_fallbacks(self, locator: Locator, page: Page, label: str) -> bool:
        """多手段点击，尽量在按钮存在时真正触发动作。"""
        methods = [
            ("scroll+click", self._click_by_standard),
            ("force_click", self._click_by_force),
            ("js_click", self._click_by_js),
            ("focus_enter", self._click_by_keyboard),
        ]
        for method_name, method in methods:
            try:
                logger.debug(f"尝试点击[{label}] 方法={method_name}")
                ok = await method(locator, page)
                if ok:
                    return True
            except Exception as err:
                logger.warning(f"点击失败[{label}] 方法={method_name} err={err}")
        return False

    async def _click_by_standard(self, locator: Locator, page: Page) -> bool:
        await locator.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)
        await locator.click(timeout=6000)
        return True

    async def _click_by_force(self, locator: Locator, page: Page) -> bool:
        await locator.scroll_into_view_if_needed()
        await page.wait_for_timeout(200)
        await locator.click(timeout=6000, force=True)
        return True

    async def _click_by_js(self, locator: Locator, page: Page) -> bool:
        await locator.evaluate("(el) => el.click()")
        return True

    async def _click_by_keyboard(self, locator: Locator, page: Page) -> bool:
        await locator.scroll_into_view_if_needed()
        await locator.focus()
        await page.keyboard.press("Enter")
        return True

    async def _verify_claim_success(self, page: Page, promotion: PromotionGame) -> bool:
        if await self._is_cloudflare_challenge_page(page):
            logger.warning("页面仍处于 Cloudflare 验证中，不能作为领取成功")
            return False

        if await self._is_claimed_on_product_page(page):
            return True

        with suppress(Exception):
            toast_text = await page.locator("[role='alert']").first.text_content() or ""
            if self._has_success_text(toast_text):
                return True

        with suppress(Exception):
            body_text = await page.locator("body").text_content() or ""
            if self._has_success_text(body_text):
                return True

        if self._order_checker:
            with suppress(Exception):
                if await self._order_checker(promotion.namespace):
                    return True

        return False

    @staticmethod
    async def _is_claimed_on_product_page(page: Page) -> bool:
        """尽量保守地判断是否已入库，避免把可领取页面误判为已拥有。"""
        owned_flags = [
            "IN LIBRARY",
            "OWNED",
            "ALREADY OWNED",
            "已拥有",
            "已在库",
            "已在库中",
            "领取成功",
            "PURCHASE SUCCESSFUL",
            "ORDER COMPLETE",
        ]
        claim_flags = [
            "GET",
            "获取",
            "领取",
            "FREE",
            "免费",
            "ADD TO LIBRARY",
            "加入库",
            "加入库中",
        ]

        # 先看购买按钮，若按钮明确显示“获取/免费”，就绝不判定为已拥有。
        with suppress(Exception):
            purchase_btn = page.locator("[data-testid='purchase-cta-button']").first
            if await purchase_btn.is_visible(timeout=2500):
                btn_text = _safe_upper(await purchase_btn.text_content())
                if any(flag in btn_text for flag in claim_flags):
                    return False
                if any(flag in btn_text for flag in owned_flags):
                    return True

        # 再看整页文本，但必须“有已拥有信号且没有领取信号”才算已拥有。
        with suppress(Exception):
            body_text = _safe_upper(await page.locator("body").text_content())
            if any(flag in body_text for flag in claim_flags):
                return False
            if any(flag in body_text for flag in owned_flags):
                return True

        return False

    async def _handle_auth_gate_flow(self, page: Page):
        logger.warning("Auth gate detected, trying to recover session.")
        with suppress(Exception):
            await page.goto(URL_CLAIM, wait_until="domcontentloaded", timeout=45000)
            return
        await self._safe_reload(page)

    async def _visual_verify_with_gemini(self, page: Page, prompt: str) -> bool:
        """使用 Gemini 视觉能力辅助验证页面状态。"""
        if not settings.GEMINI_API_KEY:
            logger.debug("GEMINI_API_KEY 未配置，跳过视觉验证")
            return False

        import base64

        try:
            screenshot_path = DEBUG_DIR / f"vision_check_{_now_tag()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            with open(screenshot_path, "rb") as f:
                img_data = base64.b64encode(f.read()).decode()

            url = (
                f"{settings.GEMINI_BASE_URL.rstrip('/')}/v1beta/models/"
                f"{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY.get_secret_value()}"
            )
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt + " 请仅回复 YES 或 NO。"},
                            {
                                "inline_data": {
                                    "mime_type": "image/png",
                                    "data": img_data,
                                }
                            },
                        ]
                    }
                ]
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=20)
                logger.debug(f"视觉验证响应状态: {resp.status_code}")
                if resp.status_code == 200:
                    answer = resp.json()["candidates"][0]["content"]["parts"][0]["text"].upper()
                    logger.debug(f"视觉验证回答: {answer}")
                    return "YES" in answer
                logger.debug(f"视觉验证失败响应: {resp.text[:500]}")
        except Exception as e:
            logger.debug(f"Visual verification failed: {e}")
        return False

    async def _click_entry_button(self, page: Page) -> str:
        """尽最大可能点击免费领取按钮，并输出足够日志定位问题。"""
        self._ensure_page_alive("click_entry_button_start")
        await self._handle_content_gate(page)

        wait_time = 20000
        logger.debug(f"Waiting up to {wait_time/1000}s for purchase area to load...")
        with suppress(Exception):
            await page.locator("[data-testid='purchase-cta-button']").first.wait_for(
                state="visible", timeout=wait_time
            )

        last_exception = None
        candidate_keywords = self._compose_candidate_keywords()
        logger.debug(f"领取按钮关键词: {candidate_keywords}")

        for idx, method in enumerate(["standard", "js", "coordinate", "keyboard"], start=1):
            self._ensure_page_alive(f"click_entry_button_attempt_{idx}")

            button = await self._find_best_button(page, candidate_keywords)

            if not button or button.get("score", 0) < 20:
                cta = page.locator("[data-testid='purchase-cta-button']").first
                with suppress(Exception):
                    if await cta.is_visible(timeout=1500):
                        text = _clean_text(await cta.text_content())
                        button = {
                            "locator": cta,
                            "text": text,
                            "source": "direct_testid",
                            "score": 100,
                            "aria": "",
                            "data-testid": "purchase-cta-button",
                        }

            if not button:
                logger.debug(f"第{idx}轮未找到按钮，等待后重试")
                await page.wait_for_timeout(2000)
                continue

            locator = button.get("locator")
            logger.debug(
                "候选按钮命中: "
                + json.dumps(
                    {
                        "attempt": idx,
                        "method": method,
                        "text": button.get("text", ""),
                        "aria": button.get("aria", ""),
                        "data-testid": button.get("data-testid", ""),
                        "source": button.get("source", ""),
                        "score": button.get("score", 0),
                    },
                    ensure_ascii=False,
                )
            )

            btn_text = _clean_text(
                f"{button.get('text', '')} {button.get('aria', '')} {button.get('data-testid', '')}"
            )
            if any(
                flag in _safe_upper(btn_text)
                for flag in ["IN LIBRARY", "OWNED", "已拥有", "已在库"]
            ):
                logger.info("Game already owned (verified by button text)")
                return btn_text

            if button.get("source") == "iframe":
                logger.success("Purchase iframe is already active; handing off to checkout flow")
                return _clean_text(f"{button.get('text', '')} {button.get('aria', '')}")

            if not locator:
                logger.debug("按钮来自 shadow dom，尝试交给 free_claim_flow")
                await self._handle_free_claim_flow(page)
                return _clean_text(f"{button.get('text', '')} {button.get('aria', '')}")

            try:
                await locator.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)

                if method == "standard":
                    logger.debug("Method: Standard Playwright click")
                    await locator.click(force=True, timeout=6000)
                elif method == "js":
                    logger.debug("Method: JavaScript click")
                    await locator.evaluate("el => el.click()")
                elif method == "coordinate":
                    logger.debug("Method: Coordinate click")
                    box = await locator.bounding_box()
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                        )
                    else:
                        logger.debug("坐标点击失败：bounding_box 为空")
                elif method == "keyboard":
                    logger.debug("Method: Focus & Enter key")
                    await locator.focus()
                    await page.keyboard.press("Enter")

                await page.wait_for_timeout(3000)
                await self._log_page_state(page, f"after_click_attempt_{idx}", None)

                if "cart" in _safe_upper(page.url) or await self._has_checkout_iframe(page):
                    logger.success(f"Get button clicked successfully via {method} method")
                    return _clean_text(f"{button.get('text', '')} {button.get('aria', '')}")

                with suppress(Exception):
                    still_visible = await locator.is_visible()
                    if not still_visible:
                        logger.success(
                            f"Get button clicked successfully via {method} method (button disappeared)"
                        )
                        return _clean_text(f"{button.get('text', '')} {button.get('aria', '')}")

                logger.warning(f"Method {method} executed but no state change detected")

            except Exception as e:
                logger.warning(f"Click method {method} failed: {e}")
                last_exception = e
                await self._dump_debug_snapshot(page, f"click_failed_{method}", None)
                await page.wait_for_timeout(1500)

        if last_exception:
            raise last_exception
        raise AssertionError("Could not find or click purchase entry button (Get/Free)")

    async def _handle_checkout_flow(self, page: Page):
        logger.info("🚀 Handling checkout flow...")
        self._ensure_page_alive("checkout_flow")
        agent = AgentV(page=page, agent_config=settings)

        wpc, payment_btn = await self._active_purchase_container(page)
        if payment_btn is None:
            raise RuntimeError("结算页按钮未找到")

        challenge_signal, cleanup_challenge_watcher = self._watch_hcaptcha_challenge(page)
        try:
            clicked = await self._click_locator_with_fallbacks(
                payment_btn, page, "checkout_payment"
            )
            if not clicked:
                logger.warning(
                    "Checkout payment button click did not report success; checking challenge state"
                )
            await page.wait_for_timeout(2000)
            await self._safe_wait_for_challenge(agent, page, challenge_signal)
        finally:
            cleanup_challenge_watcher()

        with suppress(Exception):
            await self._uk_confirm_order(wpc)

        challenge_signal, cleanup_challenge_watcher = self._watch_hcaptcha_challenge(page)
        try:
            with suppress(Exception):
                if await payment_btn.is_visible(timeout=2000) and await payment_btn.is_enabled():
                    await self._click_locator_with_fallbacks(
                        payment_btn, page, "checkout_payment_confirm"
                    )
            await self._safe_wait_for_challenge(agent, page, challenge_signal)
        finally:
            cleanup_challenge_watcher()

    async def _handle_free_claim_flow(self, page: Page):
        logger.info("🎁 Handling add-to-library flow...")
        self._ensure_page_alive("free_claim_flow")
        agent = AgentV(page=page, agent_config=settings)
        await self._handle_content_gate(page)

        try:
            await page.locator("[data-testid='purchase-cta-button']").first.wait_for(
                state="visible", timeout=12000
            )
        except Exception:
            logger.debug("Purchase button not visible after wait, proceeding with scan")

        button = await self._find_best_button(page, self._compose_candidate_keywords())
        if not button:
            cta = page.locator("[data-testid='purchase-cta-button']").first
            with suppress(Exception):
                if await cta.is_visible(timeout=2000):
                    text = _clean_text(await cta.text_content())
                    button = {"locator": cta, "text": text, "source": "direct_testid", "score": 100}

        if not button:
            raise AssertionError("Could not find Add to library/Get/Confirm button")

        btn_text = f"{button.get('text', '')} {button.get('aria', '')} {button.get('data-testid', '')}".strip()
        upper = _safe_upper(btn_text)
        if any(flag in upper for flag in ["IN LIBRARY", "OWNED", "已拥有", "已在库"]):
            logger.info("Game already owned (verified by button text)")
            return

        if button["source"] == "shadow_dom":
            logger.debug("Button found in shadow DOM, falling back to JS click discovery")
            challenge_signal, cleanup_challenge_watcher = self._watch_hcaptcha_challenge(page)
            try:
                with suppress(Exception):
                    await page.evaluate(
                        """
                        (words) => {
                          const queue = [document];
                          while (queue.length) {
                            const root = queue.shift();
                            const elements = root.querySelectorAll("button,[role='button'],[aria-label],a[role='button']");
                            for (const el of elements) {
                              const text = `${el.textContent || ""} ${el.getAttribute("aria-label") || ""} ${el.getAttribute("data-testid") || ""}`.toUpperCase();
                              const negative = ["COMING SOON", "WISHLIST", "NOTIFY ME", "UNAVAILABLE"];
                              if (negative.some((w) => text.includes(w))) continue;
                              if (words.some((w) => text.includes(w.toUpperCase()))) {
                                el.click();
                                return true;
                              }
                            }
                            const hosts = root.querySelectorAll("*");
                            for (const host of hosts) {
                              if (host.shadowRoot) queue.push(host.shadowRoot);
                            }
                          }
                          return false;
                        }
                        """,
                        self._compose_candidate_keywords(),
                    )
                await page.wait_for_timeout(2000)
                await self._safe_wait_for_challenge(agent, page, challenge_signal)
            finally:
                cleanup_challenge_watcher()
            return

        locator = button["locator"]
        if locator is None:
            raise AssertionError("Button locator is missing")

        await locator.scroll_into_view_if_needed()
        await page.wait_for_timeout(500)
        challenge_signal, cleanup_challenge_watcher = self._watch_hcaptcha_challenge(page)
        try:
            await self._click_locator_with_fallbacks(locator, page, "purchase_entry")
            await page.wait_for_timeout(2000)
            await self._safe_wait_for_challenge(agent, page, challenge_signal)
        finally:
            cleanup_challenge_watcher()

        # 点击后立刻记录状态，便于定位“点了但没反应”的原因
        await page.wait_for_timeout(1500)
        await self._log_page_state(page, "after_purchase_entry_click")

        # 如果页面没有变化，尝试更明确的按钮定位
        if "cart" not in _safe_upper(page.url):
            with suppress(Exception):
                alt_btn = page.locator("button[data-testid='purchase-cta-button']").first
                if await alt_btn.is_visible(timeout=1000):
                    logger.debug("第一次点击后仍停留原页，尝试第二次点击 purchase-cta-button")
                    challenge_signal, cleanup_challenge_watcher = self._watch_hcaptcha_challenge(
                        page
                    )
                    try:
                        await alt_btn.click(force=True)
                        await page.wait_for_timeout(2000)
                        await self._safe_wait_for_challenge(agent, page, challenge_signal)
                    finally:
                        cleanup_challenge_watcher()

    async def _find_active_purchase_container(self, page: Page):
        await page.locator(self.IFRAME_SELECTOR).first.wait_for(state="visible", timeout=20000)
        wpc = page.frame_locator(self.IFRAME_SELECTOR).first

        action_btn = wpc.locator(
            "button",
            has_text=re.compile(
                r"(add\s*to\s*library|place\s*order|submit\s*order|order\s*now|complete\s*order|pay\s*now|confirm|check\s*out)",
                re.IGNORECASE,
            ),
        )
        confirm_btn = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")

        try:
            await expect(action_btn).to_be_visible(timeout=15000)
            return wpc, action_btn.first
        except Exception:
            pass

        try:
            await expect(confirm_btn).to_be_visible(timeout=5000)
            return wpc, confirm_btn.first
        except Exception:
            raise AssertionError("Could not find checkout button in iframe")

    async def _active_purchase_container(self, page: Page):
        logger.debug("Scanning for purchase iframe...")
        return await self._find_active_purchase_container(page)

    async def _process_single_promotion(
        self, page: Page, promotion: PromotionGame
    ) -> tuple[bool, bool]:
        has_pending_cart_items = False
        result = {
            "title": promotion.title,
            "url": promotion.url,
            "flow": self.FLOW_UNKNOWN,
            "verified": False,
            "retries": 0,
            "status": "failed",
        }

        self._ensure_page_alive("process_single_promotion_start")
        page = await self._recover_page_if_closed(page, "process_single_promotion_start")
        self.page = page
        await self._log_page_state(page, "before_navigation", promotion)

        load_ok = False
        for i in range(2):
            try:
                await page.goto(promotion.url, wait_until="domcontentloaded", timeout=60000)
                await self._wait_for_cloudflare_clearance(page, promotion)
                load_ok = True
                break
            except Exception as e:
                logger.warning(f"打开详情页失败({i + 1}/2) url={promotion.url} err={e}")
                await self._dump_debug_snapshot(page, f"goto_failed_{promotion.title}", promotion)
                if i == 1:
                    self._game_results.append(result)
                    return False, False
                await page.wait_for_timeout(2000)

        if not load_ok:
            self._game_results.append(result)
            return False, False

        await self._log_page_state(page, "after_navigation", promotion)

        with suppress(Exception):
            title = await page.title()
            if "404" in title or "Page Not Found" in title:
                logger.error(f"❌ Invalid URL (404 Page): {promotion.url}")
                self._game_results.append(result)
                return False, False

        for attempt in range(1, 5):
            page = await self._recover_page_if_closed(page, f"promotion_attempt_{attempt}")
            self.page = page
            self._ensure_page_alive(f"promotion_attempt_{attempt}")
            result["retries"] = attempt - 1
            logger.info(f"开始处理促销[{promotion.title}] attempt={attempt}/4 url={promotion.url}")

            if attempt > 1:
                logger.info(f"🔄 Retrying ({attempt}/4) for {promotion.title}...")
                if attempt == 3:
                    logger.warning("Applying hard backoff: returning to free games list")
                    with suppress(Exception):
                        await page.goto(URL_CLAIM, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(5000)
                    with suppress(Exception):
                        await page.goto(promotion.url, wait_until="domcontentloaded", timeout=45000)
                else:
                    await self._safe_reload(page)

                with suppress(Exception):
                    if promotion.url.rstrip("/").split("/")[-1] not in page.url:
                        await page.goto(promotion.url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)

            await self._wait_for_cloudflare_clearance(page, promotion)
            await self._log_page_state(page, f"attempt_{attempt}_entry", promotion)

            with suppress(Exception):
                continue_btn = page.locator(
                    "//button//span[normalize-space()='Continue' or normalize-space()='继续']"
                )
                if await continue_btn.first.is_visible(timeout=1500):
                    await continue_btn.first.click()

            with suppress(Exception):
                await page.locator("[data-testid='purchase-cta-button']").first.wait_for(
                    state="visible", timeout=8000
                )

            if await self._is_claimed_on_product_page(page):
                logger.info(f"已拥有/已入库，跳过点击: {promotion.title}")
                await self._log_page_state(page, f"already_owned_skip_{promotion.title}", promotion)
                result["status"] = "already_owned"
                result["verified"] = True
                self._game_results.append(result)
                return True, False

            before_url = page.url
            before_title = None
            with suppress(Exception):
                before_title = await page.title()

            try:
                entry_text = await self._click_entry_button(page)
                logger.info(f"Entry button clicked: {entry_text}")
                if any(
                    flag in _safe_upper(entry_text)
                    for flag in ["IN LIBRARY", "OWNED", "已拥有", "已在库"]
                ):
                    result["status"] = "already_owned"
                    result["verified"] = True
                    self._game_results.append(result)
                    return True, False
            except Exception as err:
                logger.warning(f"entry click failed {promotion.url=} err={err}")
                await self._dump_debug_snapshot(
                    page, f"entry_click_failed_{promotion.title}", promotion
                )
                if await self._is_cloudflare_challenge_page(page):
                    await self._wait_for_cloudflare_clearance(page, promotion)
                    continue
                if attempt <= 2:
                    try:
                        is_get_visible = await self._visual_verify_with_gemini(
                            page,
                            "请判断页面中是否存在可点击的获取/领取/免费按钮，若存在仅回复 YES。",
                        )
                        logger.info(f"视觉辅助判断结果: {is_get_visible}")
                        if is_get_visible:
                            with suppress(Exception):
                                btn = page.locator(
                                    "button[data-testid='purchase-cta-button']"
                                ).first
                                await btn.click(force=True)
                    except Exception as visual_err:
                        logger.debug(f"视觉兜底失败: {visual_err}")

            await page.wait_for_timeout(2500)
            await self._log_page_state(page, f"attempt_{attempt}_after_click", promotion)

            flow = await self._classify_flow(page)
            result["flow"] = flow
            logger.info(
                json.dumps(
                    {
                        "title": promotion.title,
                        "flow": flow,
                        "attempt": attempt,
                        "url_before": before_url,
                        "url_after": page.url,
                        "title_before": before_title,
                    },
                    ensure_ascii=False,
                )
            )

            if "cart" in _safe_upper(page.url):
                has_pending_cart_items = True

            try:
                if flow == self.FLOW_AUTH_GATE:
                    await self._handle_auth_gate_flow(page)
                elif flow == self.FLOW_CHECKOUT:
                    await self._handle_checkout_flow(page)
                elif flow == self.FLOW_FREE_CLAIM:
                    await self._handle_free_claim_flow(page)
                else:
                    if "cart" in _safe_upper(page.url):
                        result["status"] = "added_to_cart"
                        result["verified"] = True
                        self._game_results.append(result)
                        return True, True
                    await page.wait_for_timeout(1500)
            except Exception as err:
                logger.warning(f"flow execution warning {promotion.url=} flow={flow} err={err}")
                await self._dump_debug_snapshot(page, f"flow_failed_{promotion.title}", promotion)

            verified = await self._verify_claim_success(page, promotion)
            logger.info(f"领取验证结果[{promotion.title}] verified={verified} url={page.url}")
            if verified:
                result["status"] = "claimed"
                result["verified"] = True
                self._game_results.append(result)
                return True, has_pending_cart_items

            if attempt == 3:
                logger.info("Trying 'Add to Cart' as last resort...")
                add_cart_btn = await self._find_best_button(
                    page, ["add to cart", "cart", "加入购物车"]
                )
                if add_cart_btn and add_cart_btn.get("locator") is not None:
                    with suppress(Exception):
                        await add_cart_btn["locator"].click(force=True)
                        has_pending_cart_items = True
                        await page.wait_for_timeout(2500)
                        if "cart" in _safe_upper(page.url):
                            result["status"] = "added_to_cart"
                            result["verified"] = True
                            self._game_results.append(result)
                            return True, True

            await self._dump_debug_snapshot(
                page, f"attempt_{attempt}_not_verified_{promotion.title}", promotion
            )

        result["status"] = "unverified_failed"
        self._game_results.append(result)
        return False, has_pending_cart_items

    async def add_promotion_to_cart(self, page: Page, promotions: List[PromotionGame]) -> bool:
        has_pending_cart_items = False
        for promotion in promotions:
            if self._page_is_closed(page):
                page = await self._recover_page_if_closed(page, f"promotion_loop_{promotion.title}")
                self.page = page
                if self._page_is_closed(page):
                    logger.error("页面已关闭且无法恢复，停止后续促销处理")
                    break

            try:
                _, pending = await self._process_single_promotion(page, promotion)
                has_pending_cart_items = has_pending_cart_items or pending
            except Exception as err:
                logger.warning(f"Failed to process promotion page - {promotion.url=} err={err}")
                await self._dump_debug_snapshot(
                    page, f"promotion_exception_{promotion.title}", promotion
                )
                with suppress(Exception):
                    await self._safe_reload(page)
                self._game_results.append(
                    {
                        "title": promotion.title,
                        "url": promotion.url,
                        "flow": self.FLOW_UNKNOWN,
                        "verified": False,
                        "retries": 2,
                        "status": "failed_with_exception",
                    }
                )

        return has_pending_cart_items

    async def _purchase_free_game(self):
        self._ensure_page_alive("purchase_free_game")
        await self.page.goto(URL_CART, wait_until="domcontentloaded")
        logger.debug("Proceeding with checkout for free games in cart")

        agent = AgentV(page=self.page, agent_config=settings)

        # 购物车按钮文本在不同语言/状态下会变化，做多路兜底
        checkout_selectors = [
            "//button//span[normalize-space()='Check Out']",
            "//button//span[contains(., 'Checkout')]",
            "//button//span[contains(., '继续结账')]",
            "//button//span[contains(., '结账')]",
            "//button[contains(., 'Check Out') or contains(., 'Checkout') or contains(., '结账')]",
        ]
        clicked = False
        for selector in checkout_selectors:
            with suppress(Exception):
                btn = self.page.locator(selector).first
                if await btn.is_visible(timeout=3000):
                    await btn.click(force=True)
                    clicked = True
                    logger.debug(f"已点击购物车结算按钮: {selector}")
                    break
        if not clicked:
            raise RuntimeError("未找到购物车中的 Check Out 按钮")

        await self._agree_license(self.page)

        try:
            logger.debug("Move to webPurchaseContainer iframe")
            wpc, payment_btn = await self._active_purchase_container(self.page)
            logger.debug("Click payment button")
            if payment_btn is None:
                raise RuntimeError("支付按钮不存在")
            challenge_signal, cleanup_challenge_watcher = self._watch_hcaptcha_challenge(self.page)
            try:
                await self._click_locator_with_fallbacks(payment_btn, self.page, "cart_payment")
                await self._safe_wait_for_challenge(agent, self.page, challenge_signal)
            finally:
                cleanup_challenge_watcher()
            await self._uk_confirm_order(wpc)
            await self._safe_wait_for_challenge(agent, self.page)
        except Exception as err:
            logger.warning(f"Failed to solve captcha or confirm order - {err}")
            await self._dump_debug_snapshot(self.page, "purchase_flow_failed")
            with suppress(Exception):
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
            return await self._purchase_free_game()

    @retry(retry=retry_if_exception_type(TimeoutError), stop=stop_after_attempt(2), reraise=True)
    async def collect_weekly_games(self, promotions: List[PromotionGame]):
        self._game_results = []
        has_cart_items = await self.add_promotion_to_cart(self.page, promotions)

        if has_cart_items:
            await self._purchase_free_game()
            try:
                await self.page.wait_for_url(URL_CART_SUCCESS, timeout=120000)
                logger.success("🎉 Successfully collected cart games")
            except TimeoutError:
                logger.warning("Failed to collect cart games")
        else:
            logger.info(
                "No cart items were accumulated; relying on direct claim verification only."
            )

        verified_count = len([r for r in self._game_results if r.get("verified")])
        failed_count = len([r for r in self._game_results if not r.get("verified")])
        logger.success(
            f"🎉 Process completed (verified={verified_count}, unverified={failed_count})"
        )
        logger.info(json.dumps(self._game_results, ensure_ascii=False))
