# -*- coding: utf-8 -*-
# Time       : 2022/1/16 0:25
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description: 游戏商城控制句柄

import json
import re
from contextlib import suppress
from json import JSONDecodeError
from typing import List, Callable, Awaitable

import httpx
from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import Page
from playwright.async_api import expect, TimeoutError, FrameLocator
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from models import OrderItem, Order
from models import PromotionGame
from settings import settings, RUNTIME_DIR

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"
URL_LOGIN = (
    f"https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true&redirectUrl={URL_CLAIM}"
)
URL_CART = "https://store.epicgames.com/en-US/cart"
URL_CART_SUCCESS = "https://store.epicgames.com/en-US/cart/success"


URL_PROMOTIONS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
URL_PRODUCT_PAGE = "https://store.epicgames.com/en-US/p/"
URL_PRODUCT_BUNDLES = "https://store.epicgames.com/en-US/bundles/"


def get_promotions() -> List[PromotionGame]:
    """获取周免游戏数据"""
    def is_discount_game(prot: dict) -> bool | None:
        with suppress(KeyError, IndexError, TypeError):
            offers = prot["promotions"]["promotionalOffers"][0]["promotionalOffers"]
            for i, offer in enumerate(offers):
                if offer["discountSetting"]["discountPercentage"] == 0:
                    return True

    promotions: List[PromotionGame] = []

    resp = httpx.get(URL_PROMOTIONS, params={"local": "zh-CN"})

    try:
        data = resp.json()
    except JSONDecodeError as err:
        logger.error("Failed to get promotions", err=err)
        return []

    with suppress(Exception):
        cache_key = RUNTIME_DIR.joinpath("promotions.json")
        cache_key.parent.mkdir(parents=True, exist_ok=True)
        cache_key.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # Get store promotion data and <this week free> games
    for e in data["data"]["Catalog"]["searchStore"]["elements"]:
        if not is_discount_game(e):
            continue

        # -----------------------------------------------------------
        # 🟢 智能 URL 识别逻辑
        # -----------------------------------------------------------
        is_bundle = False
        if e.get("offerType") == "BUNDLE":
            is_bundle = True
        
        # 补充检测：分类和标题
        if not is_bundle:
            for cat in e.get("categories", []):
                if "bundle" in cat.get("path", "").lower():
                    is_bundle = True
                    break
        if not is_bundle and "Collection" in e.get("title", ""):
             is_bundle = True

        base_url = URL_PRODUCT_BUNDLES if is_bundle else URL_PRODUCT_PAGE

        try:
            if e.get('offerMappings'):
                slug = e['offerMappings'][0]['pageSlug']
                e["url"] = f"{base_url.rstrip('/')}/{slug}"
            elif e.get("productSlug"):
                e["url"] = f"{base_url.rstrip('/')}/{e['productSlug']}"
            else:
                 e["url"] = f"{base_url.rstrip('/')}/{e.get('urlSlug', 'unknown')}"
        except (KeyError, IndexError):
            logger.info(f"Failed to get URL: {e}")
            continue

        logger.info(e["url"])
        promotions.append(PromotionGame(**e))

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

    async def _sync_order_history(self):
        if self._orders:
            return
        completed_orders: List[OrderItem] = []
        try:
            await self.page.goto("https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory")
            text_content = await self.page.text_content("//pre")
            data = json.loads(text_content)
            for _order in data["orders"]:
                order = Order(**_order)
                if order.orderType != "PURCHASE":
                    continue
                for item in order.items:
                    if not item.namespace or len(item.namespace) != 32:
                        continue
                    completed_orders.append(item)
        except Exception as err:
            logger.warning(err)
        self._orders = completed_orders

    async def _check_orders(self):
        await self._sync_order_history()
        self._namespaces = self._namespaces or [order.namespace for order in self._orders]
        self._promotions = [p for p in get_promotions() if p.namespace not in self._namespaces]

    async def is_namespace_claimed(self, namespace: str) -> bool:
        await self._sync_order_history()
        namespaces = [order.namespace for order in self._orders]
        return namespace in namespaces

    async def _should_ignore_task(self) -> bool:
        self._ctx_cookies_is_available = False
        await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
        status = await self.page.locator("//egs-navigation").get_attribute("isloggedin")
        if status == "false":
            logger.error("❌ context cookies is not available")
            return False
        self._ctx_cookies_is_available = True
        await self._check_orders()
        if not self._promotions:
            return True
        return False

    async def collect_epic_games(self):
        if await self._should_ignore_task():
            logger.success("All week-free games are already in the library")
            return

        if not self._ctx_cookies_is_available:
            return

        if not self._promotions:
            await self._check_orders()

        if not self._promotions:
            logger.success("All week-free games are already in the library")
            return

        for p in self._promotions:
            pj = json.dumps({"title": p.title, "url": p.url}, indent=2, ensure_ascii=False)
            logger.debug(f"Discover promotion \n{pj}")

        if self._promotions:
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
        self._promotions: List[PromotionGame] = []
        self._order_checker = order_checker
        self._game_results: List[dict] = []

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
        text_upper = text.upper()
        aria_upper = aria_label.upper()
        if keywords and any(word.upper() in text_upper for word in keywords):
            score += 40
        if keywords and any(word.upper() in aria_upper for word in keywords):
            score += 20
        if visible and enabled:
            score += 30
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
        text_upper = text.upper()
        success_flags = [
            "IN LIBRARY",
            "OWNED",
            "THANK YOU",
            "ORDER COMPLETE",
            "PURCHASE SUCCESSFUL",
        ]
        return any(flag in text_upper for flag in success_flags)

    @staticmethod
    async def _safe_wait_for_challenge(agent: AgentV, page: Page):
        challenge_markers = [
            "iframe[src*='hcaptcha']",
            "[data-hcaptcha-response]",
            "text=/I am human|hCaptcha|Verify/i",
        ]
        has_challenge = False
        for marker in challenge_markers:
            with suppress(Exception):
                if await page.locator(marker).first.is_visible(timeout=1500):
                    has_challenge = True
                    break

        if not has_challenge:
            return

        try:
            await agent.wait_for_challenge()
        except Exception as err:
            logger.warning(f"Captcha handler isolated error: {err}")

    @staticmethod
    async def _agree_license(page: Page):
        logger.debug("Agree license")
        with suppress(TimeoutError):
            await page.click("//label[@for='agree']", timeout=4000)
            accept = page.locator("//button//span[text()='Accept']")
            if await accept.is_enabled():
                await accept.click()

    @staticmethod
    async def _safe_reload(page: Page):
        with suppress(Exception):
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            return

        with suppress(Exception):
            await page.goto(URL_CLAIM, wait_until="domcontentloaded", timeout=45000)

    @staticmethod
    async def _uk_confirm_order(wpc: FrameLocator):
        logger.debug("UK confirm order")
        with suppress(TimeoutError):
            accept = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")
            if await accept.is_enabled(timeout=5000):
                await accept.click()
                return True

    async def _collect_button_candidates(self, scope, keywords: List[str], source: str) -> List[dict]:
        selectors = [
            "button",
            "[role='button']",
            "[aria-label]",
            "[data-testid='purchase-cta-button']",
        ]
        candidates: List[dict] = []
        for selector in selectors:
            locator = scope.locator(selector)
            count = min(await locator.count(), 30)
            for i in range(count):
                btn = locator.nth(i)
                try:
                    text = (await btn.text_content() or "").strip()
                    aria = (await btn.get_attribute("aria-label") or "").strip()
                    if not text and not aria:
                        continue
                    visible = await btn.is_visible()
                    enabled = await btn.is_enabled()
                    box = await btn.bounding_box()
                    in_viewport = bool(box and box.get("width", 0) > 0 and box.get("height", 0) > 0)
                    score = self._score_button_candidate(
                        text=text,
                        aria_label=aria,
                        visible=visible,
                        enabled=enabled,
                        in_viewport=in_viewport,
                        keywords=keywords,
                    )
                    candidates.append(
                        {
                            "locator": btn,
                            "text": text,
                            "aria": aria,
                            "score": score,
                            "visible": visible,
                            "enabled": enabled,
                            "source": source,
                        }
                    )
                except Exception:
                    continue
        return candidates

    async def _find_best_button(self, page: Page, keywords: List[str]) -> dict | None:
        candidates: List[dict] = []
        candidates.extend(await self._collect_button_candidates(page, keywords, "main_dom"))

        try:
            await page.locator(self.IFRAME_SELECTOR).first.wait_for(state="visible", timeout=2000)
            iframe = page.frame_locator(self.IFRAME_SELECTOR).first
            candidates.extend(await self._collect_button_candidates(iframe, keywords, "iframe"))
        except Exception:
            pass

        shadow_candidate = await page.evaluate(
            """
            (words) => {
              const queue = [document];
              const out = [];
              while (queue.length) {
                const root = queue.shift();
                const elements = root.querySelectorAll("button,[role='button'],[aria-label]");
                for (const el of elements) {
                  const text = (el.textContent || "").trim();
                  const aria = (el.getAttribute("aria-label") || "").trim();
                  if (!text && !aria) continue;
                  const full = `${text} ${aria}`.toUpperCase();
                  const score = words.some((w) => full.includes(w.toUpperCase())) ? 1 : 0;
                  if (score) {
                    out.push({ text, aria, selector: el.tagName.toLowerCase() });
                  }
                }
                const hosts = root.querySelectorAll("*");
                for (const host of hosts) {
                  if (host.shadowRoot) queue.push(host.shadowRoot);
                }
              }
              return out[0] || null;
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
                    "score": 25,
                    "visible": True,
                    "enabled": True,
                    "source": "shadow_dom",
                }
            )

        if not candidates:
            await page.wait_for_timeout(3000)
            candidates.extend(await self._collect_button_candidates(page, keywords, "main_dom_wait"))

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
        return candidates[0]

    async def _is_auth_gate_present(self, page: Page) -> bool:
        auth_selectors = [
            "#email",
            "#password",
            "#sign-in",
            "text=/Sign in|Log in/i",
        ]
        for selector in auth_selectors:
            with suppress(Exception):
                if await page.locator(selector).first.is_visible(timeout=1200):
                    return True
        return False

    async def _has_checkout_iframe(self, page: Page) -> bool:
        with suppress(Exception):
            return await page.locator(self.IFRAME_SELECTOR).first.is_visible(timeout=1500)
        return False

    async def _classify_flow(self, page: Page) -> str:
        has_auth_gate = await self._is_auth_gate_present(page)
        has_checkout_iframe = await self._has_checkout_iframe(page)
        free_candidate = await self._find_best_button(page, ["add to library", "confirm", "get"])
        has_free_claim_action = bool(free_candidate and free_candidate.get("score", 0) > 0)
        return self._classify_flow_signals(
            has_auth_gate=has_auth_gate,
            has_checkout_iframe=has_checkout_iframe,
            has_free_claim_action=has_free_claim_action,
        )

    async def _active_purchase_container(self, page: Page):
        logger.debug("Scanning for purchase iframe...")
        await page.locator(self.IFRAME_SELECTOR).first.wait_for(state="visible", timeout=20000)
        wpc = page.frame_locator(self.IFRAME_SELECTOR).first

        action_btn = wpc.locator(
            "button",
            has_text=re.compile(
                r"(place\s*order|submit\s*order|order\s*now|complete\s*order|pay\s*now|confirm)",
                re.IGNORECASE,
            ),
        )
        confirm_btn = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")

        try:
            await expect(action_btn).to_be_visible(timeout=15000)
            return wpc, action_btn.first
        except AssertionError:
            pass

        try:
            await expect(confirm_btn).to_be_visible(timeout=5000)
            return wpc, confirm_btn.first
        except AssertionError:
            raise AssertionError("Could not find checkout button in iframe")

    async def _handle_checkout_flow(self, page: Page):
        logger.info("🚀 Handling checkout flow...")
        agent = AgentV(page=page, agent_config=settings)
        wpc, payment_btn = await self._active_purchase_container(page)
        await payment_btn.click(force=True)
        await page.wait_for_timeout(2500)
        await self._safe_wait_for_challenge(agent, page)
        with suppress(Exception):
            await self._uk_confirm_order(wpc)
            await payment_btn.click(force=True)

    async def _handle_free_claim_flow(self, page: Page):
        logger.info("🎁 Handling add-to-library flow...")
        button = await self._find_best_button(page, ["add to library", "confirm", "get"])
        if not button:
            raise AssertionError("Could not find Add to library/Get/Confirm button")

        if button["source"] == "shadow_dom":
            await page.evaluate(
                """
                (words) => {
                  const queue = [document];
                  while (queue.length) {
                    const root = queue.shift();
                    const elements = root.querySelectorAll("button,[role='button'],[aria-label]");
                    for (const el of elements) {
                      const text = `${el.textContent || ""} ${el.getAttribute("aria-label") || ""}`.toUpperCase();
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
                ["add to library", "confirm", "get"],
            )
            return

        await button["locator"].click(force=True)
        await page.wait_for_timeout(1500)
        second_button = await self._find_best_button(page, ["add to library", "confirm", "place order"])
        if second_button and second_button["locator"] is not None and second_button["score"] > 35:
            with suppress(Exception):
                await second_button["locator"].click(force=True)

    @staticmethod
    async def _is_claimed_on_product_page(page: Page) -> bool:
        with suppress(Exception):
            purchase_btn = page.locator("//button[@data-testid='purchase-cta-button']").first
            if await purchase_btn.is_visible(timeout=3000):
                btn_text = (await purchase_btn.text_content() or "").upper()
                if any(s in btn_text for s in ["IN LIBRARY", "OWNED"]):
                    return True

        with suppress(Exception):
            body_text = (await page.locator("body").text_content() or "").upper()
            if "IN LIBRARY" in body_text or "OWNED" in body_text:
                return True

        return False

    async def _verify_claim_success(self, page: Page, promotion: PromotionGame) -> bool:
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

    async def _handle_auth_gate_flow(self, page: Page):
        logger.warning("Auth gate detected, trying to recover session.")
        with suppress(Exception):
            await page.goto(URL_CLAIM, wait_until="domcontentloaded", timeout=45000)
            return
        await self._safe_reload(page)

    async def _click_entry_button(self, page: Page) -> str:
        button = await self._find_best_button(page, ["get", "add to library", "cart", "purchase", "free"])
        if not button:
            raise AssertionError("Could not find purchase entry button")
        btn_text = f"{button.get('text', '')} {button.get('aria', '')}".strip()
        if button["source"] == "shadow_dom":
            await self._handle_free_claim_flow(page)
            return btn_text
        await button["locator"].click(force=True)
        return btn_text

    async def _process_single_promotion(self, page: Page, promotion: PromotionGame) -> tuple[bool, bool]:
        has_pending_cart_items = False
        result = {
            "title": promotion.title,
            "url": promotion.url,
            "flow": self.FLOW_UNKNOWN,
            "verified": False,
            "retries": 0,
            "status": "failed",
        }

        await page.goto(promotion.url, wait_until="domcontentloaded", timeout=60000)
        title = await page.title()
        if "404" in title or "Page Not Found" in title:
            logger.error(f"❌ Invalid URL (404 Page): {promotion.url}")
            self._game_results.append(result)
            return False, False

        for attempt in range(1, 4):
            result["retries"] = attempt - 1
            with suppress(Exception):
                continue_btn = page.locator("//button//span[text()='Continue']")
                if await continue_btn.is_visible(timeout=3000):
                    await continue_btn.click()

            if await self._is_claimed_on_product_page(page):
                result["status"] = "already_owned"
                result["verified"] = True
                self._game_results.append(result)
                return True, False

            try:
                entry_text = await self._click_entry_button(page)
                if "CART" in entry_text.upper():
                    has_pending_cart_items = True
                    result["status"] = "added_to_cart"
                    result["verified"] = True
                    self._game_results.append(result)
                    return True, True
            except Exception as err:
                logger.warning(f"entry click failed {promotion.url=} err={err}")

            flow = await self._classify_flow(page)
            result["flow"] = flow
            logger.info(
                json.dumps(
                    {
                        "title": promotion.title,
                        "flow": flow,
                        "attempt": attempt,
                        "url": promotion.url,
                    },
                    ensure_ascii=False,
                )
            )

            try:
                if flow == self.FLOW_AUTH_GATE:
                    await self._handle_auth_gate_flow(page)
                elif flow == self.FLOW_CHECKOUT:
                    await self._handle_checkout_flow(page)
                elif flow == self.FLOW_FREE_CLAIM:
                    await self._handle_free_claim_flow(page)
                else:
                    await page.wait_for_timeout(2500)
            except Exception as err:
                logger.warning(f"flow execution warning {promotion.url=} flow={flow} err={err}")

            verified = await self._verify_claim_success(page, promotion)
            if verified:
                result["status"] = "claimed"
                result["verified"] = True
                self._game_results.append(result)
                return True, has_pending_cart_items

            if attempt == 1:
                await self._safe_reload(page)
            elif attempt == 2:
                add_cart_btn = await self._find_best_button(page, ["add to cart", "cart"])
                if add_cart_btn and add_cart_btn.get("locator") is not None:
                    with suppress(Exception):
                        await add_cart_btn["locator"].click(force=True)
                        has_pending_cart_items = True
                        await page.goto(URL_CART, wait_until="domcontentloaded")
            else:
                result["status"] = "unverified_failed"
                self._game_results.append(result)

        return False, has_pending_cart_items

    async def add_promotion_to_cart(self, page: Page, promotions: List[PromotionGame]) -> bool:
        has_pending_cart_items = False
        for promotion in promotions:
            try:
                _, pending = await self._process_single_promotion(page, promotion)
                has_pending_cart_items = has_pending_cart_items or pending
            except Exception as err:
                logger.warning(f"Failed to process promotion page - {promotion.url=} err={err}")
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

    async def _empty_cart(self, page: Page, wait_rerender: int = 30) -> bool | None:
        has_paid_free = False
        try:
            cards = await page.query_selector_all("//div[@data-testid='offer-card-layout-wrapper']")
            for card in cards:
                is_free = await card.query_selector("//span[text()='Free']")
                if not is_free:
                    has_paid_free = True
                    wishlist_btn = await card.query_selector(
                        "//button//span[text()='Move to wishlist']"
                    )
                    await wishlist_btn.click()

            if has_paid_free and wait_rerender:
                wait_rerender -= 1
                await page.wait_for_timeout(2000)
                return await self._empty_cart(page, wait_rerender)
            return True
        except TimeoutError as err:
            logger.warning("Failed to empty shopping cart", err=err)
            return False

    async def _click_cart_checkout(self, page: Page) -> None:
        candidate = await self._find_best_button(
            page, ["check out", "checkout", "place order", "order now"]
        )
        if candidate and candidate.get("locator") is not None and candidate.get("score", 0) > 0:
            await candidate["locator"].click(force=True)
            return

        fallback_selectors = [
            "button:has-text('Check Out')",
            "button:has-text('Checkout')",
            "a:has-text('Check Out')",
            "a:has-text('Checkout')",
            "//button[contains(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'CHECK OUT') or contains(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'CHECKOUT')]",
            "//a[contains(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'CHECK OUT') or contains(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'CHECKOUT')]",
        ]
        for selector in fallback_selectors:
            with suppress(Exception):
                checkout = page.locator(selector).first
                if await checkout.is_visible(timeout=1200) and await checkout.is_enabled():
                    await checkout.click(force=True)
                    return

        raise TimeoutError("Could not find checkout action on cart page")

    async def _purchase_free_game(self):
        await self.page.goto(URL_CART, wait_until="domcontentloaded")
        logger.debug("Move ALL paid games from the shopping cart out")
        await self._empty_cart(self.page)

        agent = AgentV(page=self.page, agent_config=settings)
        await self._click_cart_checkout(self.page)
        await self._agree_license(self.page)

        try:
            logger.debug("Move to webPurchaseContainer iframe")
            wpc, payment_btn = await self._active_purchase_container(self.page)
            logger.debug("Click payment button")
            await self._uk_confirm_order(wpc)
            await self._safe_wait_for_challenge(agent, self.page)
        except Exception as err:
            logger.warning(f"Failed to solve captcha - {err}")
            await self.page.reload()
            return await self._purchase_free_game()

    @retry(retry=retry_if_exception_type(TimeoutError), stop=stop_after_attempt(2), reraise=True)
    async def collect_weekly_games(self, promotions: List[PromotionGame]):
        self._game_results = []
        has_cart_items = await self.add_promotion_to_cart(self.page, promotions)

        if has_cart_items:
            await self._purchase_free_game()
            try:
                await self.page.wait_for_url(URL_CART_SUCCESS)
                logger.success("🎉 Successfully collected cart games")
            except TimeoutError:
                logger.warning("Failed to collect cart games")

        verified_count = len([r for r in self._game_results if r.get("verified")])
        failed_count = len([r for r in self._game_results if not r.get("verified")])
        logger.success(
            f"🎉 Process completed (verified={verified_count}, unverified={failed_count})"
        )
        logger.info(json.dumps(self._game_results, ensure_ascii=False))
