# -*- coding: UTF-8 -*-
"""Purchase-flow helpers for DamaiBot.

Methods relocated from ``mobile/damai_app.py`` (W4-01 split, zero behavior
change).  Hosts the detail→purchase entry path and the fast submit retry loop.
"""

from __future__ import annotations

import logging
import time

from . import (
    _SALE_READY_TEXT_REGEX_OR,
    logger,
)

try:
    from mobile.logger import log_event, log_strategy, log_wait
except ImportError:  # pragma: no cover
    from logger import log_event, log_strategy, log_wait  # type: ignore[no-redef]

try:
    from mobile.ui_primitives import ANDROID_UIAUTOMATOR
except ImportError:  # pragma: no cover
    from ui_primitives import ANDROID_UIAUTOMATOR  # type: ignore[no-redef]

try:
    from selenium.webdriver.common.by import By
except ModuleNotFoundError:  # pragma: no cover
    raise


class PurchaseFlowMixin:
    """Mixin contributing detail→purchase entry and submit logic to ``DamaiBot``."""

    def _enter_purchase_flow_from_detail_page(self, prepared=False):
        """Open the purchase panel from the detail page with a low-latency hot path."""
        cached_buy = (
            self._cached_hot_path_coords.get("detail_buy")
            if self.config.rush_mode and not prepared
            else None
        )
        if cached_buy:
            # 开售轮询已经量过底部按钮。用设备点击，点完只看 Activity。
            logger.info("点击购票按钮...")
            return self._click_into_purchase(cached_buy)
        if getattr(self, "_sale_hot_path", False) and not prepared:
            logger.info("点击购票按钮...")
            point = self._bottom_bar_point()
            if point is not None:
                return self._click_into_purchase(point)
            if not self._click_detail_purchase_bar():
                logger.warning("购票按钮点击失败")
                return None
            entered = self._poll_purchase_entry_by_activity(timeout=0.7)
            if entered:
                return entered
            logger.info("点击后仍在详情页，再点一次购票按钮")
            if not self._click_detail_purchase_bar():
                return None
            return self._poll_purchase_entry_by_activity(timeout=0.5)
        if self.config.rush_mode:
            self._dismiss_fast_blocking_dialogs()
        if not prepared:
            if self.config.rush_mode:
                # 极速模式冷路径：单次 XML dump 提取所有坐标（~0.3s），替代多次 _cached_tap（~3-4s）。
                # 热路径（有缓存）用 _cached_tap 直接点击缓存坐标（1次 HTTP/元素）。
                if self._using_u2() and not self._cached_hot_path_coords.get(
                    "detail_buy"
                ):
                    # Cold path: single XML dump for all detail page elements.
                    if self._rush_preselect_and_buy_via_xml():
                        next_probe = self._wait_for_purchase_entry_result(
                            timeout=6.0, poll_interval=0.03
                        )
                        if next_probe["state"] in {"sku_page", "order_confirm_page"}:
                            return next_probe
                else:
                    # Warm path: cached coords for date/city/buy.
                    if (
                        self.config.date
                        and "date" not in self._cached_hot_path_no_match
                    ):
                        _date_found = self._cached_tap(
                            "date",
                            ANDROID_UIAUTOMATOR,
                            f'new UiSelector().textContains("{self.config.date}")',
                            timeout=0.1,
                        )
                        if _date_found:
                            logger.info(f"极速模式预选日期: {self.config.date}")
                        elif "date" not in self._cached_hot_path_coords:
                            self._cached_hot_path_no_match.add("date")
                    if (
                        self.config.city
                        and "city" not in self._cached_hot_path_no_match
                    ):
                        _city_found = self._cached_tap(
                            "city",
                            ANDROID_UIAUTOMATOR,
                            f'new UiSelector().text("{self.config.city}")',
                            timeout=0.2,
                        )
                        if not _city_found:
                            _city_found = self._cached_tap(
                                "city",
                                ANDROID_UIAUTOMATOR,
                                f'new UiSelector().textContains("{self.config.city}")',
                                timeout=0.15,
                            )
                        if _city_found:
                            logger.info(f"极速模式预选城市: {self.config.city}")
                        elif "city" not in self._cached_hot_path_coords:
                            self._cached_hot_path_no_match.add("city")
                            logger.debug("极速模式未命中城市选择，继续抢占购票入口")
            else:
                self.select_performance_date()
                logger.info("选择城市...")
                if not self._select_city_from_detail_page(timeout=1.0):
                    logger.warning("城市选择失败")
                    return None

        if not self._cached_hot_path_coords.get("detail_buy"):
            logger.info("点击购票按钮...")
        if self.config.rush_mode:
            # 极速模式：_cached_tap 冷路径查找并缓存购票按钮坐标，热路径直接点击（1次HTTP）。
            # 点击一次后等足够长时间，避免重复点击重置 sku_page 加载。
            _buy_clicked = self._cached_tap(
                "detail_buy",
                By.ID,
                "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl",
                timeout=0.2,
            )
            if not _buy_clicked:
                # 文案集合源 SALE_READY_TEXTS（issue #29）+ 旧文案兜底
                _buy_clicked = self._cached_tap(
                    "detail_buy",
                    ANDROID_UIAUTOMATOR,
                    f'new UiSelector().textMatches("{_SALE_READY_TEXT_REGEX_OR}|.*购票.*|.*抢票.*|.*购买.*")',
                    timeout=0.25,
                )
            if _buy_clicked:
                next_probe = self._wait_for_purchase_entry_result(
                    timeout=6.0, poll_interval=0.03
                )
                if next_probe["state"] in {"sku_page", "order_confirm_page"}:
                    return next_probe

        # 文案集合源 SALE_READY_TEXTS（issue #29）+ 旧"预约/购买"兜底
        book_selectors = [
            (
                By.ID,
                "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl",
            ),
            (
                ANDROID_UIAUTOMATOR,
                f'new UiSelector().textMatches("{_SALE_READY_TEXT_REGEX_OR}|.*预约.*|.*购买.*")',
            ),
            (By.XPATH, '//*[contains(@text,"预约") or contains(@text,"购买")]'),
        ]
        if not self.smart_wait_and_click(
            *book_selectors[0], book_selectors[1:], timeout=0.8
        ):
            logger.warning("购票按钮点击失败")
            return None
        return self._wait_for_purchase_entry_result(timeout=5, poll_interval=0.08)

    def _click_into_purchase(self, point):
        """点开售按钮。没离开详情页就再点一次，仍停在详情页则返回空。"""
        self._click_coordinates(*point, duration=25)
        entered = self._poll_purchase_entry_by_activity(timeout=0.7)
        if entered:
            return entered
        logger.info("点击后仍在详情页，再点一次购票按钮")
        self._click_coordinates(*point, duration=25)
        return self._poll_purchase_entry_by_activity(timeout=0.5)

    def _poll_purchase_entry_by_activity(self, timeout=0.7):
        """点进场次页后只读 Activity。没进场次页或确认页就返回空，避免在详情页上选票。"""
        t0 = time.monotonic()
        planned_ms = int(max(0.0, float(timeout)) * 1000)
        last = None
        state = None
        try:
            deadline = time.monotonic() + timeout
            while True:
                last = self._wait_detail_hot_entry()
                state = (last or {}).get("state")
                if state in {"sku_page", "order_confirm_page"}:
                    return last
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.02)
        finally:
            waited_ms = int((time.monotonic() - t0) * 1000)
            log_wait(
                logger,
                kind="purchase_entry_poll",
                waited_ms=waited_ms,
                planned_ms=planned_ms,
                state=state or "timeout",
                level=logging.DEBUG
                if state in {"sku_page", "order_confirm_page"} and waited_ms < 100
                else logging.INFO,
            )

    def _click_labeled_button_coordinates(self, selectors):
        """按按钮边界中心坐标点击，点击方式和详情页「立即预订」一致。"""
        for by, value in selectors or ():
            try:
                element = self._find(by, value)
            except Exception:
                continue
            if not self._selector_exists(element):
                continue
            if self._tap_element_center_coordinates(element, duration=25):
                return True
        return False

    def _click_bottom_bar_point(self):
        """点屏幕底部购票区域。同一次坐标能点中详情页购票栏和票档页底部按钮。"""
        point = self._bottom_bar_point()
        if point is None:
            return False
        self._click_coordinates(*point, duration=25)
        return True

    def _click_right_bottom_bar_point(self, burst_count=1):
        """固定相对坐标点票档「确定」或确认页「立即提交」，不查控件。"""
        point = self._right_bottom_bar_point()
        if point is None:
            return False
        if burst_count > 1:
            self._burst_click_coordinates(
                *point, count=burst_count, interval_ms=25, duration=25
            )
        else:
            self._click_coordinates(*point, duration=25)
        return True

    # 「抢票人数太多」弹窗主按钮；勿点下方「返回重新选购」。
    _SUBMIT_BUSY_RETRY_SELECTORS = (
        (ANDROID_UIAUTOMATOR, 'new UiSelector().text("继续尝试")'),
        (ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("继续尝试")'),
    )

    # 弹窗主按钮大约在屏幕垂直中部；相对坐标兜底，避免元素查找过慢。
    _SUBMIT_BUSY_RETRY_RELATIVE = (0.50, 0.58)

    def _submit_busy_retry_point(self):
        """人数过多弹窗「继续尝试」的固定相对坐标。"""
        size = self._screen_size_tuple()
        if not size:
            return None
        rx, ry = self._SUBMIT_BUSY_RETRY_RELATIVE
        return (int(size[0] * rx), int(size[1] * ry))

    def _click_submit_busy_retry_dialog(self):
        """点确认页「人数过多」弹窗的「继续尝试」。出现则点，否则返回 False。"""
        for by, value in self._SUBMIT_BUSY_RETRY_SELECTORS:
            if not self._has_element(by, value):
                continue
            if self._click_labeled_button_coordinates(((by, value),)):
                return True
            if self.ultra_fast_click(by, value, timeout=0.15):
                return True
        # 文案可见但坐标点击失败时，用相对坐标再打一次。
        if self._has_element(
            ANDROID_UIAUTOMATOR,
            'new UiSelector().textContains("抢票人数太多")',
        ) or self._has_element(
            ANDROID_UIAUTOMATOR,
            'new UiSelector().text("继续尝试")',
        ):
            point = self._submit_busy_retry_point()
            if point is not None:
                self._click_coordinates(*point, duration=25)
                return True
        return False

    def _click_detail_purchase_bar(self):
        """点详情页底部购票栏，不读整页界面树。"""
        try:
            bar = self._find(
                By.ID,
                "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl",
            )
        except Exception:
            return False
        if not self._selector_exists(bar):
            return False
        return self._tap_element_center_coordinates(bar, duration=25)

    def _wait_detail_hot_entry(self):
        """点完只查一次 Activity。正式抢票走确认按钮，不把票档页当成预约页。"""
        activity = self._get_current_activity() or ""
        if "DmOrder" in activity:
            return {"state": "order_confirm_page", "submit_button": True}
        if "NcovSku" in activity:
            return {
                "state": "sku_page",
                "price_container": True,
                "reservation_mode": False,
            }
        if "ProjectDetail" in activity:
            return {"state": "detail_page"}
        return self.probe_current_page(fast=True)

    def _tap_submit_once(self, submit_selectors):
        """点一次「立即提交」：热路径右下坐标，其它路径走文案/选择器。"""
        if getattr(self, "_sale_hot_path", False):
            if self._click_right_bottom_bar_point():
                return "right_bottom_bar_point"
            if self._click_labeled_button_coordinates(submit_selectors):
                return "labeled_button_coords"
            self._click_bottom_bar_point()
            return "bottom_bar_point"

        if submit_selectors and self.ultra_fast_click(
            *submit_selectors[0], timeout=0.35
        ):
            return "ultra_fast_primary"
        if (
            submit_selectors
            and len(submit_selectors) > 1
            and self.ultra_fast_click(*submit_selectors[1], timeout=0.35)
        ):
            return "ultra_fast_secondary"
        if submit_selectors and self.smart_wait_and_click(
            *submit_selectors[0], submit_selectors[1:], timeout=0.6
        ):
            return "smart_wait"
        return None

    def _submit_order_fast(self, submit_selectors):
        """提交确认页订单；遇「人数过多」弹窗则连点「继续尝试」直到进支付页。"""
        # 安全上限，避免真·死循环；人数过多场景下会持续点「继续尝试」。
        deadline_s = 180.0
        t0 = time.monotonic()
        deadline = t0 + deadline_s
        attempt = 0
        busy_clicks = 0
        has_submitted_once = False
        hot = getattr(self, "_sale_hot_path", False)

        while time.monotonic() < deadline:
            attempt += 1
            tap_t0 = time.monotonic()

            if self._click_submit_busy_retry_dialog():
                busy_clicks += 1
                log_strategy(
                    logger,
                    action="submit_order",
                    strategy="busy_retry_continue",
                    attempt=attempt,
                    busy_clicks=busy_clicks,
                    duration_ms=int((time.monotonic() - tap_t0) * 1000),
                )
                if busy_clicks == 1 or busy_clicks % 5 == 0:
                    logger.info(
                        "检测到人数过多弹窗，点击继续尝试（第 %d 次）",
                        busy_clicks,
                    )
            else:
                strategy = self._tap_submit_once(submit_selectors)
                if strategy is None:
                    if not has_submitted_once and not busy_clicks:
                        logger.warning("提交订单按钮未找到，请手动确认订单状态")
                        log_event(
                            logger,
                            "order_submitted",
                            level=logging.WARNING,
                            success=False,
                            result="button_not_found",
                            attempts=attempt,
                            duration_ms=int((time.monotonic() - t0) * 1000),
                        )
                        return "timeout"
                    # 已点过提交/继续尝试：再等一轮结果，不立刻放弃。
                    strategy = "wait_result"
                else:
                    has_submitted_once = True
                log_strategy(
                    logger,
                    action="submit_order",
                    strategy=strategy,
                    attempt=attempt,
                    busy_clicks=busy_clicks,
                    duration_ms=int((time.monotonic() - tap_t0) * 1000),
                )

            verify_timeout = 0.45 if hot else (1.2 if attempt < 3 else 2.0)
            if hot:
                result = self._verify_order_result_hot(timeout=verify_timeout)
            else:
                result = self.verify_order_result(timeout=verify_timeout)
            if result != "timeout":
                log_event(
                    logger,
                    "order_submitted",
                    success=result not in ("timeout", "failed"),
                    result=result,
                    attempts=attempt,
                    busy_clicks=busy_clicks,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
                return result

            # 验证窗口内可能刚弹出人数过多；下一轮优先点「继续尝试」。
            if attempt == 1 or attempt % 10 == 0:
                logger.info(
                    "支付页尚未出现，继续提交/重试 %d（已点继续尝试 %d 次）",
                    attempt + 1,
                    busy_clicks,
                )

        log_event(
            logger,
            "order_submitted",
            level=logging.WARNING,
            success=False,
            result="timeout",
            attempts=attempt,
            busy_clicks=busy_clicks,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return "timeout"
