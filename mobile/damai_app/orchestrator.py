# -*- coding: UTF-8 -*-
"""DamaiBot orchestrator: __init__, main run loops, and helper methods that
remain on the bot class after W4-01 split.

Module constants (``SALE_READY_TEXTS`` etc.), the package-level ``logger``
and shared dependency classes are imported from :mod:`mobile.damai_app`'s
``__init__`` (the package).  Hot-path helpers split into mixins live in
sibling submodules: ``sale_waiter``, ``purchase_flow``,
``recovery_strategies`` and ``coords_cache``.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from selenium.webdriver.common.by import By

try:
    from mobile.env_snapshot import detect_damai_app_version
    from mobile.logger import log_event, log_step, log_wait
except ImportError:  # pragma: no cover
    from env_snapshot import detect_damai_app_version  # type: ignore[no-redef]
    from logger import log_event, log_step, log_wait  # type: ignore[no-redef]

from . import (
    ANDROID_UIAUTOMATOR,
    AttendeeSelector,
    BuyButtonGuard,
    Config,
    EventNavigator,
    FastPipeline,
    PageProbe,
    PageState,
    PriceSelector,
    PriceSelectorError,
    RecoveryHelper,
    SessionNotFoundError,
    SoldOutError,
    UIPrimitives,
    logger,
    select_session,
)
from .coords_cache import CoordsCacheMixin
from .delegators import DelegatorsMixin
from .purchase_flow import PurchaseFlowMixin
from .recovery_strategies import RecoveryStrategiesMixin
from .sale_waiter import SaleWaiterMixin
from .state_probe import StateProbeMixin


class DamaiBot(
    SaleWaiterMixin,
    PurchaseFlowMixin,
    RecoveryStrategiesMixin,
    CoordsCacheMixin,
    StateProbeMixin,
    DelegatorsMixin,
    UIPrimitives,
):
    def __init__(self, config=None, setup_driver=True):
        self.config = config or Config.load_config()
        self.item_detail = None
        self.driver = None
        self.d = None
        self.wait = None
        self._terminal_failure_reason = None
        self._last_run_outcome = None
        # U-12：本次 run_with_retry 的总执行轮次（外层尝试与快速重试各 +1），
        # 供运行摘要 attempts 字段消费；纯整数自增，零热路径开销。
        self._attempts_made = 0
        self._last_discovery_step_timings = []
        self._last_run_step_timings = []
        # Cache of {key: (x, y)} coordinates for hot-path elements.
        # Populated on the first run and reused on warm retries (1 HTTP call vs 4+).
        self._cached_hot_path_coords: dict = {}
        # Keys of preselect steps that failed on a previous run and should be skipped.
        self._cached_hot_path_no_match: set = set()
        self._prepare_runtime_config()
        if setup_driver:
            self._setup_driver()

        # Sub-modules that work with any backend (Appium or u2)
        _device = self.d or self.driver
        self._attendee_sel = AttendeeSelector(_device, self.config)
        self._attendee_sel.set_bot(self)
        self._price_sel = PriceSelector(_device, self.config, probe=None)
        self._price_sel.set_bot(self)
        self._navigator = EventNavigator(_device, self.config, probe=None)
        self._navigator.set_bot(self)

        # Sub-modules for u2-optimized operations
        if self.d is not None:
            self._page_probe = PageProbe(self.d, self.config)
            self._page_probe.set_bot(self)
            self._guard = BuyButtonGuard(self.d)
            self._pipeline = FastPipeline(
                self.d, self.config, self._page_probe, self._guard
            )
            self._pipeline.set_bot(self)
            # Share coordinate cache with pipeline
            self._pipeline._cached_coords = self._cached_hot_path_coords
            self._pipeline._cached_no_match = self._cached_hot_path_no_match
            # Update sub-modules with real probe now that it exists
            self._navigator._probe = self._page_probe
            self._recovery = RecoveryHelper(self.d, self._page_probe, self._navigator)
            self._price_sel._probe = self._page_probe

        if setup_driver:
            self._emit_boot_snapshot()

    def _emit_boot_snapshot(self) -> None:
        """Emit a one-shot ``event=boot`` log line with environment metadata."""
        try:
            serial = getattr(self.config, "serial", None) or getattr(
                self.config, "device_serial", None
            )
            damai_version = detect_damai_app_version(serial=serial) or "unknown"
            log_event(
                logger,
                "boot",
                py_version=f"{sys.version_info.major}.{sys.version_info.minor}",
                damai_version=damai_version,
                device=serial or "unknown",
                rush_mode=bool(getattr(self.config, "rush_mode", False)),
            )
        except Exception:  # pragma: no cover — boot logging must never crash startup
            logger.debug("boot snapshot failed (suppressed)", exc_info=True)

    def _ensure_pipeline(self):
        """Lazily create the FastPipeline if not yet initialised (e.g. in tests)."""
        if hasattr(self, "_pipeline"):
            return
        device = self.d or self.driver
        probe = getattr(self, "_page_probe", None)
        guard = getattr(self, "_guard", None)
        self._pipeline = FastPipeline(device, self.config, probe, guard)
        self._pipeline.set_bot(self)
        self._pipeline._cached_coords = self._cached_hot_path_coords
        self._pipeline._cached_no_match = self._cached_hot_path_no_match

    _SOLD_OUT_RE = re.compile(r"缺货|售罄|无票")

    _BUY_BUTTON_NODE_IDS = ("btn_buy_view", "cn.damai:id/btn_buy_view")
    _BUY_BAR_CONTAINER_NODE_IDS = (
        "trade_project_detail_purchase_status_bar_container_fl",
        "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl",
    )

    def _node_shows_sold_out(self, node):
        """判断单个 XML 节点的 text/content-desc 是否包含缺货文案。"""
        text = node.get("text", "")
        desc = node.get("content-desc", "")
        return bool(
            self._SOLD_OUT_RE.search(text) or self._SOLD_OUT_RE.search(desc)
        )

    def _is_buy_button_sold_out(self):
        """Check if the buy button itself shows sold-out text.

        Only inspects the buy button / purchase-bar element, not the whole
        page, to avoid false positives from other price tiers showing '缺货登记'.

        优先匹配 v8.x 的 btn_buy_view（有文案，可判缺货）；仅当其在整棵树中
        不存在时才回退到 ≥9.0.2x 的购买栏容器（issue #41）。注意 iter("node")
        是文档序（父先于子），容器很可能是 btn_buy_view 的祖先，不能在首个
        命中节点上早退，否则 v8.x 的缺货文本会被空文案容器短路漏检。
        容器为 Canvas 自绘（text/content-desc 均空）时返回 False，不误判缺货。
        """
        xml_root = self._dump_hierarchy_xml()
        if xml_root is None:
            return False
        container_node = None
        for node in xml_root.iter("node"):
            rid = node.get("resource-id", "")
            if rid in self._BUY_BUTTON_NODE_IDS:
                return self._node_shows_sold_out(node)
            if container_node is None and rid in self._BUY_BAR_CONTAINER_NODE_IDS:
                container_node = node
        if container_node is not None:
            return self._node_shows_sold_out(container_node)
        return False

    def _set_terminal_failure(self, reason):
        """Mark the current failure as non-retriable."""
        self._terminal_failure_reason = reason

    def _set_run_outcome(self, outcome):
        """Record the terminal outcome for the latest run attempt."""
        self._last_run_outcome = outcome

    def _report_pending_order_dialog(self):
        """检测到「未支付订单弹窗」时的统一上报（真话优先，U-10 同源）。

        probe_only 从不提交任何订单，此弹窗必然是账号里预先存在的占单（非本次
        安全探测产生）。旧实现无论何种模式都记 ``order_pending_payment`` →
        「抢票成功：检测到未支付订单」，会误导安全探测用户以为 probe 下了单
        （2026-07-11 真机实测）。据实分流，返回 True 语义不变：
        - probe_only：``preexisting_pending_order`` —— 只做探测提醒，不称成功；
        - 正式/开发模式：``order_pending_payment`` —— 本轮流程占单，沿用原语义。
        """
        if self.config.probe_only:
            self._set_run_outcome("preexisting_pending_order")
            logger.info(
                "探测提醒：账号存在未支付订单（非本次安全探测产生），"
                "如需可自行前往订单页处理；本次探测未做任何提交"
            )
        else:
            self._set_run_outcome("order_pending_payment")
            logger.info(
                "检测到未支付订单弹窗（已占单待支付），请立即前往订单页完成支付"
            )
        return True

    def _execution_mode_key(self):
        """Return the current execution mode key."""
        if self.config.probe_only:
            return "probe"
        if not self.config.if_commit_order:
            return "validation"
        return "submit"

    def _execution_mode_label(self):
        """Return a short user-facing label for the current execution mode."""
        labels = {
            "probe": "安全探测",
            "validation": "开发验证",
            "submit": "正式抢票",
        }
        return labels[self._execution_mode_key()]

    def _execution_mode_description(self):
        """Return a user-facing description for the current execution mode."""
        descriptions = {
            "probe": "会走与正式抢票相同的下单流程，但不会点击“立即提交”",
            "validation": "会继续进入确认页并勾选观演人，但不会点击“立即提交”；这是开发调试路径",
            "submit": "会尝试提交订单",
        }
        return descriptions[self._execution_mode_key()]

    def _log_execution_mode(self):
        """Emit a clear log that tells the user what this run will actually do."""
        logger.info(
            f"开始执行{self._execution_mode_label()}：{self._execution_mode_description()}"
        )

    def _log_success_outcome(self, retry_prefix=""):
        """Emit a success log message that matches the actual run outcome."""
        prefix = f"{retry_prefix}" if retry_prefix else ""
        outcome_messages = {
            "probe_ready": "探测成功：已走完下单流程，未提交订单",
            "validation_ready": "开发验证成功：已到订单确认页，未提交订单",
            "order_submitted": "抢票成功：已提交订单",
            "order_pending_payment": "抢票成功：检测到未支付订单，请立即前往支付完成下单",
            "preexisting_pending_order": "探测提醒：账号存在未支付订单（非本次探测产生），本次未做任何提交",
            "order_flow_completed": "抢票流程完成：已执行提交，等待后续结果确认",
        }
        logger.info(
            f"{prefix}{outcome_messages.get(self._last_run_outcome, '本轮执行成功')}"
        )

    def _finalize_submit_result(self, result, *, start_time=None):
        """_submit_order_fast 结果 → (返回值, outcome/terminal) 的唯一映射。

        主路径 run_ticket_grabbing 与快速重试 order_confirm_page 分支共用，
        保证两条提交路径对同一 verify 结果语义一致（U-10）——
        「成功」只能由 verify_order_result 确认，点击成功不算数。
        耗时信息仅在提供 start_time 时输出（主路径口径）。
        """
        elapsed_suffix = (
            "" if start_time is None else f"耗时: {time.time() - start_time:.2f}秒"
        )
        if result == "success":
            self._set_run_outcome("order_submitted")
            logger.info(f"抢票成功！{elapsed_suffix}")
            return True
        if result == "existing_order":
            self._set_run_outcome("order_pending_payment")
            logger.info(
                f"检测到未支付订单（已占单待支付），请立即前往订单页支付。{elapsed_suffix}"
            )
            return True
        if result in ("sold_out", "captcha"):
            # 失败路径也记录真实结局，禁止虚报成功；不设 terminal，保留可重试语义。
            self._set_run_outcome(result)
            return False
        # timeout / 未知值 — fail closed，避免虚假成功与重复提交
        self._set_terminal_failure("submit_unverified")
        logger.error(
            f"提交后未能确认成功状态（result={result}），"
            f"为避免重复下单已停止自动重试，请手动检查订单列表。{elapsed_suffix}"
        )
        return False

    @contextmanager
    def _timed_step(self, step_name, manual_baseline_seconds=None):
        """Record and log per-step latency for discovery / commit hot path."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            duration_ms = int(elapsed * 1000)
            faster_than_manual = (
                True
                if manual_baseline_seconds is None
                else elapsed <= manual_baseline_seconds
            )
            entry = {
                "step": step_name,
                "seconds": round(elapsed, 3),
                "duration_ms": duration_ms,
                "manual_baseline_seconds": manual_baseline_seconds,
                "faster_than_manual": faster_than_manual,
            }
            self._last_discovery_step_timings.append(entry)
            self._last_run_step_timings.append(entry)
            extra = {}
            if manual_baseline_seconds is not None:
                extra["manual_baseline_ms"] = int(manual_baseline_seconds * 1000)
                extra["faster_than_manual"] = faster_than_manual
            log_step(
                logger,
                step_name,
                duration_ms=duration_ms,
                level=logging.INFO
                if faster_than_manual
                else logging.WARNING,
                **extra,
            )

    def _sleep_logged(self, seconds, kind):
        """带日志的 sleep：记录计划等待与实际等待。"""
        planned_ms = int(max(0.0, float(seconds)) * 1000)
        if planned_ms <= 0:
            log_wait(logger, kind=kind, waited_ms=0, planned_ms=0, skipped=True)
            return
        t0 = time.monotonic()
        time.sleep(seconds)
        log_wait(
            logger,
            kind=kind,
            waited_ms=int((time.monotonic() - t0) * 1000),
            planned_ms=planned_ms,
        )

    def _log_step_timing_summary(self):
        """打印本轮主流程各步骤耗时汇总。"""
        steps = getattr(self, "_last_run_step_timings", None) or []
        if not steps:
            return
        total_ms = sum(int(item.get("duration_ms") or 0) for item in steps)
        parts = [
            f"{item.get('step')}={int(item.get('duration_ms') or 0)}ms" for item in steps
        ]
        logger.info(
            "步骤耗时汇总 total=%dms steps=%d | %s",
            total_ms,
            len(steps),
            " | ".join(parts),
        )
        log_event(
            logger,
            "step_timing_summary",
            total_ms=total_ms,
            steps=len(steps),
            detail=";".join(parts),
        )

    def _prepare_runtime_config(self):
        """Pre-flight config checks before creating the driver session."""
        pass

    def _setup_driver(self):
        """初始化 uiautomator2 直连驱动。"""
        import uiautomator2 as u2

        serial = getattr(self.config, "serial", None) or None
        self.d = u2.connect(serial)
        try:
            self.d.settings["wait_timeout"] = 0
            self.d.settings["operation_delay"] = (0, 0)
        except Exception:
            # 测试桩或精简驱动对象可能不支持 dict-style settings
            pass
        try:
            width, height = self.d.window_size()
            self._screen_size = (int(width), int(height))
        except Exception:
            self._screen_size = None
        should_start_app = True
        try:
            current_app = self.d.app_current()
            if (
                isinstance(current_app, dict)
                and current_app.get("package") == self.config.app_package
            ):
                should_start_app = False
        except Exception:
            should_start_app = True

        if should_start_app:
            self.d.app_start(
                self.config.app_package,
                activity=self.config.app_activity,
                stop=False,
            )
        self.driver = self.d
        self.wait = None

    # Core element operations, click operations, element inspection, and selector
    # utilities are inherited from UIPrimitives (mobile/ui_primitives.py).

    def wait_for_page_state(self, expected_states, timeout=5, poll_interval=0.2):
        """轮询等待页面进入指定状态，返回最后一次探测结果。"""
        deadline = time.time() + timeout
        last_probe = None

        while time.time() < deadline:
            last_probe = self.probe_current_page(fast=True)
            if last_probe["state"] in expected_states:
                return last_probe
            time.sleep(poll_interval)

        return last_probe if last_probe is not None else self.probe_current_page()

    def _wait_for_purchase_entry_result(
        self, timeout=1.2, poll_interval=0.04, fallback_probe_on_timeout=True
    ):
        """Wait for the detail-page CTA to open either sku or confirm page."""
        if self.config.rush_mode:
            # 极速模式：每次轮询只用 1 个 ID 选择器（~60ms/轮），替代 4 个选择器（~240ms/轮）。
            # 先检查 sku（更常见的转换目标），再检查确认页 checkbox。
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self._has_element(By.ID, "cn.damai:id/layout_sku"):
                    # 探测也要识别预约页。以前探测直接写成 False，
                    # 会在「预约想看」页上继续点票档，白等一轮。
                    return {
                        "state": "sku_page",
                        "price_container": True,
                        "reservation_mode": self.is_reservation_sku_mode(),
                    }
                if self._has_element(By.ID, "cn.damai:id/checkbox"):
                    return {"state": "order_confirm_page", "submit_button": True}
                time.sleep(poll_interval)
            if fallback_probe_on_timeout:
                return self.probe_current_page(fast=True)
            return None

        submit_selectors = [
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("立即提交")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches(".*提交.*|.*确认.*")'),
            (By.XPATH, '//*[contains(@text,"提交")]'),
        ]
        sku_selectors = [
            (By.ID, "cn.damai:id/project_detail_perform_price_flowlayout"),
            (By.ID, "cn.damai:id/layout_sku"),
            (By.ID, "cn.damai:id/sku_contanier"),
            (By.ID, "cn.damai:id/layout_price"),
            (By.ID, "cn.damai:id/tv_price_name"),
        ]

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_any_element(submit_selectors):
                return {"state": "order_confirm_page", "submit_button": True}
            if self._has_any_element(sku_selectors):
                return {
                    "state": "sku_page",
                    "price_container": True,
                    "reservation_mode": self.is_reservation_sku_mode(),
                }
            time.sleep(poll_interval)

        if fallback_probe_on_timeout:
            return self.probe_current_page(fast=True)
        return None

    def _wait_for_submit_ready(self, timeout=1.6, poll_interval=0.04):
        """Wait until the confirm-page submit button appears."""
        t0 = time.monotonic()
        planned_ms = int(max(0.0, float(timeout)) * 1000)
        ready = False
        try:
            activity = self._get_current_activity() or ""
            if "DmOrder" in activity:
                ready = True
                return True
            if self.config.rush_mode:
                # 极速模式：单 ID 选择器轮询。订单页没有旧版 checkbox，每轮都看 Activity。
                deadline = time.time() + timeout
                while time.time() < deadline:
                    activity = self._get_current_activity() or ""
                    if "DmOrder" in activity:
                        ready = True
                        return True
                    if self._has_element(By.ID, "cn.damai:id/checkbox"):
                        ready = True
                        return True
                    time.sleep(poll_interval)
                return False

            submit_selectors = [
                (ANDROID_UIAUTOMATOR, 'new UiSelector().text("立即提交")'),
                (
                    ANDROID_UIAUTOMATOR,
                    'new UiSelector().textMatches(".*提交.*|.*确认.*")',
                ),
                (By.XPATH, '//*[contains(@text,"提交")]'),
            ]

            deadline = time.time() + timeout
            while time.time() < deadline:
                if self._has_any_element(submit_selectors):
                    ready = True
                    return True
                time.sleep(poll_interval)

            return False
        finally:
            waited_ms = int((time.monotonic() - t0) * 1000)
            # 确认循环里短轮询很密，短等待降到 DEBUG，避免刷屏。
            wait_level = (
                logging.DEBUG
                if ready and waited_ms < 50 and planned_ms < 200
                else logging.INFO
            )
            log_wait(
                logger,
                kind="submit_ready",
                waited_ms=waited_ms,
                planned_ms=planned_ms,
                ready=ready,
                level=wait_level,
            )

    def _click_sku_buy_button_element(self, burst_count=1):
        """点票档页右侧「确定」。与详情页「立即预订」一样只走坐标点击。"""
        try:
            buy_button = self._find(By.ID, "cn.damai:id/btn_buy_view")
        except Exception:
            return False
        if not self._selector_exists(buy_button):
            return False
        return self._tap_element_center_coordinates(
            buy_button, burst_count=burst_count, duration=25, interval_ms=25
        )

    # ------------------------------------------------------------------
    # Failure diagnostics (P1 #31)
    # ------------------------------------------------------------------

    def _save_price_failure_dump(self, reason: str = "") -> Path | None:
        """Persist a UI hierarchy XML (and best-effort screenshot) to tmp/.

        Called when the price selection path fails so post-mortem analysis
        does not need a live device. ``tmp/`` is git-ignored.
        Returns the dump path on success, or ``None`` if nothing could be
        captured (e.g. driver unavailable).
        """
        try:
            tmp_dir = Path("tmp")
            tmp_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            xml_path = tmp_dir / f"price_dump_{ts}.xml"

            xml_text = ""
            try:
                if self._using_u2():
                    xml_text = self.d.dump_hierarchy()
            except Exception as exc:  # pragma: no cover - logged only
                logger.warning("dump_hierarchy 失败: %s", exc)
                xml_text = ""

            if not xml_text:
                logger.warning("无法获取 UI hierarchy，跳过 dump 落盘")
                return None

            xml_path.write_text(xml_text, encoding="utf-8")
            suffix = f"（reason={reason}）" if reason else ""
            logger.error("failure dump saved to %s%s", xml_path, suffix)

            # best-effort screenshot — never block the dump on screenshot failure
            png_path = xml_path.with_suffix(".png")
            try:
                if self._using_u2() and hasattr(self.d, "screenshot"):
                    self.d.screenshot(str(png_path))
                    logger.error("failure screenshot saved to %s", png_path)
            except Exception as exc:  # pragma: no cover - logged only
                logger.debug("screenshot 不可用: %s", exc)

            return xml_path
        except Exception as exc:  # pragma: no cover - logged only
            logger.warning("保存 price dump 失败: %s", exc)
            return None

    def select_performance_date(self, timeout=1.0):
        """选择演出场次日期"""
        if not self.config.date:
            return

        date_selector = f'new UiSelector().textContains("{self.config.date}")'
        if self.ultra_fast_click(ANDROID_UIAUTOMATOR, date_selector, timeout=timeout):
            logger.info(f"选择场次日期: {self.config.date}")
        else:
            logger.debug(f"未找到日期 '{self.config.date}'，使用默认场次")

    def _handle_session_picker(self):
        """SESSION_PICKER 状态下按 config.date / config.city 选定场次。

        优先级与 :func:`mobile.event_navigator.select_session` 一致：
        date+city > date 唯一 > fallback_index(0)。多场次场景下永不跳过，
        即便 rush_skip_session=True 也强制选场（issue #25 根因防御）。
        """
        try:
            from mobile.date_utils import normalize_date
        except ImportError:  # pragma: no cover
            from date_utils import normalize_date  # type: ignore[no-redef]

        if getattr(self.config, "rush_skip_session", False):
            logger.warning(
                "rush_skip_session=True 但当前为多场次场次选择页 — 强制选场避免抢错（issue #25）"
            )

        normalised_date = normalize_date(self.config.date) if self.config.date else None
        try:
            chosen_idx = select_session(
                self.d,
                date=normalised_date,
                city=self.config.city,
                fallback_index=0,
            )
            logger.info(
                "多场次活动：已选定场次 idx=%d (date=%s, city=%s)",
                chosen_idx,
                normalised_date,
                self.config.city,
            )
            self._session_list_logged = True
            return True
        except SessionNotFoundError as exc:
            logger.error("多场次活动：select_session 失败 — %s", exc)
            self._set_terminal_failure("session_not_found")
            return False

    def _select_city_from_detail_page(self, timeout=1.0):
        """Select the configured city on the detail page."""
        city_selectors = [
            (ANDROID_UIAUTOMATOR, f'new UiSelector().text("{self.config.city}")'),
            (
                ANDROID_UIAUTOMATOR,
                f'new UiSelector().textContains("{self.config.city}")',
            ),
            (By.XPATH, f'//*[@text="{self.config.city}"]'),
        ]
        return self.smart_wait_and_click(
            *city_selectors[0], city_selectors[1:], timeout=timeout
        )

    def _wait_reservation_page_for_sale(self):
        """预约页等待开抢。开抢前的睡眠和高频轮询仍走 wait_for_sale_start。

        页面变成可购买后返回 True。没有开抢时间，或开抢后仍是预约，
        立刻返回 False，调用方马上高频点击底部按钮。
        """
        if self.config.sell_start_time is None:
            return False
        logger.info(
            "当前是预约页，等待抢购；开抢前 %d ms 进入高频轮询",
            self.config.countdown_lead_ms,
        )
        self.wait_for_sale_start()
        if not self.is_reservation_sku_mode():
            return True
        return False

    def _click_reservation_bottom_button(self):
        """点预约页底部按钮，不读整页界面树。"""
        try:
            button = self._find(By.ID, "cn.damai:id/btn_buy_view")
        except Exception:
            return False
        if not self._selector_exists(button):
            return False
        return self._tap_element_center_coordinates(button, burst_count=1)

    def _hammer_reservation_bottom_button(self):
        """预约流程里持续高频点击底部按钮，直到离开预约页或进入确认页。"""
        logger.info("已进入预售/抢票预约流程，高频点击底部按钮")
        coords = self._cached_hot_path_coords.get("sku_buy")
        clicks = 0
        while True:
            clicks += 1
            clicked = False
            if not coords:
                clicked = self._click_bottom_bar_point()
                if not clicked:
                    clicked = self._click_reservation_bottom_button()
                if not clicked:
                    try:
                        coords = self._get_buy_button_coordinates()
                    except Exception:
                        coords = None
                    if coords:
                        self._cached_hot_path_coords["sku_buy"] = coords
            if coords:
                self._burst_click_coordinates(
                    *coords, count=2, interval_ms=20, duration=20
                )
            elif not clicked:
                try:
                    button = self._d(resourceId="cn.damai:id/btn_buy_view")
                    if not self._tap_element_center_coordinates(button):
                        self.ultra_fast_click(
                            By.ID, "cn.damai:id/btn_buy_view", timeout=0.15
                        )
                except Exception:
                    logger.debug("预约页底部按钮点击失败", exc_info=True)
            if clicks == 1 or clicks % 20 == 0:
                logger.info("预约页底部按钮已高频点击 %d 次", clicks)
            if clicks % 5 != 0:
                continue
            if getattr(self, "_sale_hot_path", False):
                activity = self._get_current_activity() or ""
                if "DmOrder" in activity or any(
                    key in activity for key in ("Pay", "Cashier", "AlipayClient")
                ):
                    logger.info("底部按钮已进入订单确认页")
                    return "confirm"
                continue
            if self._wait_for_submit_ready(timeout=0.05, poll_interval=0.01):
                logger.info("底部按钮已进入订单确认页")
                return "confirm"
            if not self.is_reservation_sku_mode():
                logger.info("预约页已切换，底部按钮共点击 %d 次", clicks)
                return "sku"

    def _prepare_detail_page_hot_path(self):
        """Preselect detail-page filters before the sale opens so launch-time work is minimized."""
        page_probe = self.probe_current_page()
        if page_probe["state"] != "detail_page":
            return False

        prepared = False
        if self.config.date:
            self.select_performance_date()
            prepared = True

        if self.config.city and self._select_city_from_detail_page(timeout=0.6):
            logger.info(f"已预选城市: {self.config.city}")
            prepared = True

        return prepared

    def check_session_valid(self):
        """检查大麦 App 登录状态是否有效"""
        activity = self._get_current_activity()
        if "LoginActivity" in activity or "SignActivity" in activity:
            logger.error("检测到登录页面，大麦 App 登录已过期，请重新登录")
            return False

        login_prompt_selectors = [
            'new UiSelector().textContains("请先登录")',
            'new UiSelector().textContains("登录/注册")',
        ]
        for selector in login_prompt_selectors:
            if self._has_element(ANDROID_UIAUTOMATOR, selector):
                logger.error("检测到登录提示，请重新登录大麦 App")
                return False

        return True

    def verify_order_result(self, timeout=5):
        """验证订单提交结果"""
        start = time.time()
        payment_text_selectors = [
            'new UiSelector().textContains("立即支付")',
            'new UiSelector().textContains("去支付")',
            'new UiSelector().textContains("确认支付")',
            'new UiSelector().textContains("支付剩余时间")',
            'new UiSelector().textContains("收银台")',
        ]

        while time.time() - start < timeout:
            activity = self._get_current_activity()

            # Success: payment page
            if any(kw in activity for kw in ("Pay", "Cashier", "AlipayClient")):
                logger.info("订单提交成功，已跳转支付页面")
                return "success"

            # Check page text for various outcomes
            if self._has_element(
                ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("未支付")'
            ):
                logger.warning("已有未支付订单")
                return "existing_order"
            if (
                self._has_element(
                    ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("已售罄")'
                )
                or self._has_element(
                    ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("库存不足")'
                )
                or self._has_element(
                    ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("暂时无票")'
                )
            ):
                logger.warning("票已售罄")
                return "sold_out"
            if self._has_element(
                ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("滑块")'
            ) or self._has_element(
                ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("验证")'
            ):
                logger.warning("触发验证码")
                return "captcha"
            if any(
                self._has_element(ANDROID_UIAUTOMATOR, selector)
                for selector in payment_text_selectors
            ):
                submit_still_visible = self._has_element(
                    ANDROID_UIAUTOMATOR,
                    'new UiSelector().text("立即提交")',
                )
                confirm_title_visible = self._has_element(
                    ANDROID_UIAUTOMATOR,
                    'new UiSelector().textContains("确认购买")',
                )
                if submit_still_visible or confirm_title_visible:
                    logger.warning(
                        "检测到支付相关文本，但仍在确认购买页，暂不判定提交成功"
                    )
                else:
                    logger.info("订单提交成功，检测到支付页关键控件")
                    return "success"

            # 人数过多弹窗：点「继续尝试」后继续等支付页，不要点「返回重新选购」。
            if self._click_submit_busy_retry_dialog():
                logger.info("订单验证中检测到人数过多弹窗，已点击继续尝试")
                continue

            time.sleep(0.3)

        logger.warning("订单验证超时")
        return "timeout"

    def _verify_order_result_hot(self, timeout=0.35):
        """开售热路径只看支付页 Activity，不再逐条扫描支付文案。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            activity = self._get_current_activity() or ""
            if any(kw in activity for kw in ("Pay", "Cashier", "AlipayClient")):
                logger.info("订单提交成功，已跳转支付页面")
                return "success"
            time.sleep(0.04)
        return "timeout"

    def dismiss_startup_popups(self):
        """处理首启的一次性系统/应用弹窗。"""
        dismissed = False

        popup_clicks = [
            (By.ID, "android:id/ok"),  # Android 全屏提示
            (By.ID, "cn.damai:id/id_boot_action_agree"),  # 大麦隐私协议
            (By.ID, "cn.damai:id/damai_theme_dialog_cancel_btn"),  # 开启消息通知
            (
                By.ID,
                "cn.damai:id/damai_theme_dialog_close_layout",
            ),  # 新版升级提示关闭按钮
            (By.ID, "cn.damai:id/damai_theme_dialog_close_btn"),  # 新版升级提示关闭图标
            (
                ANDROID_UIAUTOMATOR,
                'new UiSelector().text("Cancel")',
            ),  # Add to home screen
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("下次再说")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("我知道了")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("知道了")'),
        ]

        for by, value in popup_clicks:
            if self._has_element(by, value):
                if self.ultra_fast_click(by, value):
                    dismissed = True
                    time.sleep(0.3)

        return dismissed

    def _dismiss_fast_blocking_dialogs(self):
        """Dismiss lightweight blocking dialogs without the full startup scan."""
        dismissed = False
        dialog_clicks = [
            (By.ID, "cn.damai:id/damai_theme_dialog_cancel_btn"),
            (By.ID, "cn.damai:id/damai_theme_dialog_close_btn"),
            (By.ID, "cn.damai:id/damai_theme_dialog_close_layout"),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("知道了")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("我知道了")'),
        ]

        for by, value in dialog_clicks:
            if not self._has_element(by, value):
                continue
            if self.ultra_fast_click(by, value, timeout=0.15):
                dismissed = True
                time.sleep(0.05)

        return dismissed

    def _uses_validation_shortcuts(self):
        """开发验证极速捷径。probe 与 commit 不走这里。

        probe 与正式抢票共用同一套下单步骤，唯一差别是 probe 不点「立即提交」。
        """
        return (not self.config.if_commit_order) and (not self.config.probe_only)

    def _should_submit_order(self):
        """只有正式抢票才点击「立即提交」。"""
        return bool(self.config.if_commit_order) and not bool(self.config.probe_only)

    def _trust_page_defaults(self):
        """探测和正式抢票都不核对场次、观演人，直接用页面当前默认项。"""
        return bool(self.config.probe_only or self.config.if_commit_order)

    def _log_visible_sessions_and_prices(self, xml_root=None):
        """打印当前页场次列表和价位列表。读取失败不中断下单。"""
        try:
            # 点开票档后的第一次 dump 经常还是动画中的空树。坐标重试已经看到
            # 「缺货登记」时，列表仍拿着那棵旧树，就会打印成未识别。
            from mobile.price_selector import hierarchy_has_sku_cards

            if self._using_u2() and not hierarchy_has_sku_cards(xml_root):
                fresh = self._dump_hierarchy_xml()
                if hierarchy_has_sku_cards(fresh):
                    xml_root = fresh
            elif xml_root is None and self._using_u2():
                xml_root = self._dump_hierarchy_xml()
            if not getattr(self, "_session_list_logged", False):
                sessions = []
                try:
                    sessions = (
                        self.get_visible_session_options(
                            xml_root=xml_root, allow_ocr=self.config.probe_only
                        )
                        or []
                    )
                except Exception:
                    sessions = []
                if not sessions:
                    try:
                        sessions = [
                            {"index": index, "text": date_text, "tag": ""}
                            for index, date_text in enumerate(
                                self.get_visible_date_options(xml_root=xml_root) or []
                            )
                        ]
                    except Exception:
                        sessions = []
                if sessions:
                    logger.info("场次列表（共 %d 个）:", len(sessions))
                    for item in sessions:
                        tag = item.get("tag") or ""
                        text = item.get("text") or "(未识别场次)"
                        suffix = f" [{tag}]" if tag else ""
                        logger.info("  [%s] %s%s", item.get("index"), text, suffix)
                else:
                    logger.info("场次列表: 当前页面未识别到场次")
                self._session_list_logged = True

            options = []
            try:
                options = (
                    self.get_visible_price_options(
                        allow_ocr=self.config.probe_only, xml_root=xml_root
                    )
                    or []
                )
            except Exception:
                options = []
            self._visible_price_options = options
            if options:
                logger.info("价位列表（共 %d 个）:", len(options))
                for option in options:
                    tag = option.get("tag") or "可选"
                    text = option.get("text") or "(未识别)"
                    logger.info("  [%s] %s [%s]", option.get("index"), text, tag)
            else:
                logger.info("价位列表: 当前页面未识别到价位")
        except Exception:
            logger.info("场次/价位列表读取失败，继续下单流程")

    def _log_visible_attendees(self):
        """打印确认页可见观演人。读取失败不中断下单。"""
        try:
            names = []
            seen = set()
            xml_root = self._dump_hierarchy_xml() if self._using_u2() else None
            visible = []
            if xml_root is not None:
                for node in xml_root.iter("node"):
                    if node.get("visible-to-user") == "false":
                        continue
                    resource_id = node.get("resource-id") or ""
                    for attr in ("text", "content-desc"):
                        text = (node.get(attr) or "").strip()
                        if text and text not in seen:
                            seen.add(text)
                            visible.append((resource_id, text))
                            if resource_id == "cn.damai:id/text_name" or resource_id.endswith(
                                "/text_name"
                            ):
                                names.append(text)
            if not names:
                for element in self._find_all(By.ID, "cn.damai:id/text_name"):
                    text = (self._read_element_text(element) or "").strip()
                    if text and text not in seen:
                        seen.add(text)
                        names.append(text)
            configured = [str(user) for user in (self.config.users or [])]
            if not names:
                for _resource_id, text in visible:
                    if any(user and user in text and len(text) <= 20 for user in configured):
                        names.append(text)
            if names:
                logger.info("人员列表（页面可见 %d 人）:", len(names))
                for index, name in enumerate(names):
                    mark = " ← 配置选中" if any(user and user in name for user in configured) else ""
                    logger.info("  [%d] %s%s", index, name, mark)
            else:
                logger.info(
                    "人员列表: 页面未识别到观演人；配置 users=%s",
                    "、".join(configured) or "(空)",
                )
                if visible:
                    logger.info("页面可见文本（共 %d 条）:", len(visible))
                    for index, (_resource_id, text) in enumerate(visible[:40]):
                        logger.info("  [%d] %s", index, text)
        except Exception:
            logger.info("人员列表读取失败，继续下单流程")

    def run_ticket_grabbing(self, initial_page_probe=None):
        """执行抢票主流程"""
        try:
            start_time = time.time()
            self._terminal_failure_reason = None
            self._last_run_outcome = None
            self._session_list_logged = False
            self._visible_price_options = None
            self._last_run_step_timings = []
            self._log_execution_mode()
            with self._timed_step("probe_current_page"):
                page_probe = initial_page_probe or self.probe_current_page(fast=True)
            validation_shortcut = self._uses_validation_shortcuts()
            fast_validation_hot_path = (
                validation_shortcut
                and self.config.rush_mode
                and initial_page_probe is not None
                and page_probe["state"] in {"detail_page", "sku_page"}
            )
            if fast_validation_hot_path:
                logger.info(
                    "开发验证极速路径：跳过启动弹窗与登录探测，直接执行抢票热路径"
                )
                if page_probe["state"] == "detail_page":
                    if self._has_warm_pipeline_coords():
                        # Warm pipeline: blind shell clicks + concurrent polling.
                        pipeline_result = self._run_warm_validation_pipeline(start_time)
                    elif self._using_u2():
                        # Cold pipeline: XML dump → shell batch → concurrent polling.
                        pipeline_result = self._run_cold_validation_pipeline(start_time)
                    else:
                        pipeline_result = None
                    if pipeline_result is True:
                        return True
                    if pipeline_result is False:
                        return False
                    # pipeline_result is None → fall through to normal flow.
                    # Refresh page_probe since the pipeline may have advanced
                    # the app state (e.g. from detail_page to sku_page).
                    page_probe = self.probe_current_page(fast=True)
            elif self.config.rush_mode and (
                page_probe["state"] == "sku_page"
                or (
                    page_probe["state"] == "detail_page"
                    and self._trust_page_defaults()
                )
            ):
                # 已经在详情页或票档页。9 个弹窗选择器每次都要问一遍设备，
                # 详情页上实测约 1.5s，而且一个都点不中。
                pass
            else:
                self.dismiss_startup_popups()
                if not self.check_session_valid():
                    self._set_terminal_failure("session_invalid")
                    return False

            if page_probe["state"] == "pending_order_dialog":
                return self._report_pending_order_dialog()

            if page_probe["state"] not in {
                "detail_page",
                "sku_page",
                PageState.SESSION_PICKER.value,
            } or (
                self.item_detail and not self._current_page_matches_target(page_probe)
            ):
                if self.config.auto_navigate:
                    logger.info("当前不在目标演出页，尝试自动导航")
                    if not self.navigate_to_target_event(page_probe):
                        return False
                    page_probe = self.probe_current_page()
                    if page_probe["state"] == "pending_order_dialog":
                        return self._report_pending_order_dialog()
                else:
                    logger.warning("当前不在演出详情页，请先手动打开目标演出详情页")
                    return False

            prepared_detail_page = False
            # 探测和正式抢票信任页面默认场次，不再预选日期/城市，也不再多探两次详情页。
            should_prepare_detail_page = (
                page_probe["state"] == "detail_page"
                and not self._trust_page_defaults()
                and (
                    self.config.sell_start_time is not None
                    or (
                        self.config.wait_cta_ready_timeout_ms > 0
                        and not self.config.rush_mode
                    )
                )
            )
            if should_prepare_detail_page:
                prepared_detail_page = self._prepare_detail_page_hot_path()
                page_probe = self.probe_current_page()

            # Wait for sale start if configured
            self._sale_hot_path = bool(
                self.config.rush_mode and self._trust_page_defaults()
            )
            with self._timed_step("wait_for_sale_start"):
                self.wait_for_sale_start()
            # 开售时间已过时再做一次全量探测，只会在详情页上多等几秒。
            if not (self.config.rush_mode and self._trust_page_defaults()) and (
                self.config.sell_start_time is not None or not self.config.rush_mode
            ):
                with self._timed_step("probe_after_sale_wait"):
                    page_probe = self.probe_current_page()

            if page_probe["state"] == "detail_page":
                with self._timed_step("enter_purchase_from_detail"):
                    page_probe = self._enter_purchase_flow_from_detail_page(
                        prepared=prepared_detail_page
                    )
                if page_probe is None or page_probe.get("state") == "detail_page":
                    logger.info("点击后仍在详情页，本次不选票价")
                    self._log_step_timing_summary()
                    return False
                if self._trust_page_defaults():
                    logger.info("使用页面默认场次，不核对日期")
            else:
                logger.info("当前已在票档选择页，跳过城市和预约按钮步骤")
                # 新版 SKU 页会先展示日期卡片，需在此再次选择场次后才会展开票档列表。
                if self._trust_page_defaults():
                    logger.info("使用页面默认场次，不核对日期")
                elif validation_shortcut and self.config.rush_mode:
                    logger.info("开发验证极速路径：已在票档页，跳过场次切换")
                else:
                    with self._timed_step("ensure_performance_date"):
                        self._ensure_performance_date_selected(
                            max_wait=2.0 if self.config.rush_mode else 3.0
                        )
            if self.config.rush_mode:
                # 极速模式下避免一次完整重探测，减少热路径阻塞。
                # 预约不在这里连打选择器，交给下面同一次界面树判断。
                page_probe = dict(page_probe)
                page_probe.setdefault("state", "sku_page")
            else:
                page_probe = self.probe_current_page()

            # 多场次活动：识别 SESSION_PICKER 后强制选场，避免抢错场次（issue #25）。
            # 探测 / 正式抢票改为信任页面默认场次，不再在这里停下来比对日期。
            if (
                page_probe["state"] == PageState.SESSION_PICKER.value
                and not self._trust_page_defaults()
            ):
                if not self._handle_session_picker():
                    return False
                page_probe = self.probe_current_page()

            price_coords = (
                page_probe.get("price_coords") if self.config.rush_mode else None
            )
            buy_button_coords = (
                page_probe.get("buy_button_coords") if self.config.rush_mode else None
            )
            # 热路径优先从 bot 级缓存读取坐标，避免重复 XML dump（热重试节省 ~0.5s）。
            if self.config.rush_mode:
                if price_coords is None:
                    price_coords = self._cached_hot_path_coords.get("price")
                if buy_button_coords is None:
                    buy_button_coords = self._cached_hot_path_coords.get("sku_buy")
            _sku_xml = None
            if getattr(self, "_sale_hot_path", False) and page_probe.get("state") == "sku_page":
                # 正式抢票不把票档页当成预约页。底部右侧是「确定」，不是预约按钮。
                page_probe["reservation_mode"] = False
                # 固定右下相对坐标，不查 btn_buy_view（查找常耗 0.5–1s）。
                if buy_button_coords is None:
                    buy_button_coords = self._right_bottom_bar_point()
            elif self.config.rush_mode and page_probe["state"] == "sku_page":
                if (
                    price_coords is None
                    or buy_button_coords is None
                    or not page_probe.get("reservation_mode")
                ):
                    # 票档坐标和预约标记共用这一次界面树。
                    _sku_xml = self._dump_hierarchy_xml()
                    if not page_probe.get("reservation_mode"):
                        page_probe["reservation_mode"] = self.reservation_marked_in_xml(
                            _sku_xml
                        )
                    if price_coords is None:
                        price_coords = (
                            self._get_price_option_coordinates_by_config_index(
                                xml_root=_sku_xml
                            )
                        )
                    if buy_button_coords is None:
                        buy_button_coords = self._get_buy_button_coordinates(
                            xml_root=_sku_xml
                        )
            self._sku_hierarchy_xml = _sku_xml

            reservation_reached_confirm = False
            if page_probe["state"] == "sku_page" and page_probe.get("reservation_mode"):
                # 开售时间已过时，5 个预约选择器再查一遍会把底部点击拖过 1 秒。
                if getattr(self, "_sale_hot_path", False):
                    if (
                        self.config.sell_start_time
                        and not self._sell_start_already_passed()
                    ):
                        self.wait_for_sale_start()
                    reached = self._hammer_reservation_bottom_button()
                elif self._wait_reservation_page_for_sale():
                    reached = None
                    page_probe["reservation_mode"] = False
                    logger.info("预约页已开抢，继续原来的下单流程")
                else:
                    reached = self._hammer_reservation_bottom_button()
                if reached == "confirm":
                    page_probe["reservation_mode"] = False
                    reservation_reached_confirm = True
                    page_probe["state"] = "order_confirm_page"
                    page_probe["submit_button"] = True
                elif reached == "sku":
                    page_probe["reservation_mode"] = False
            # 更新 bot 级缓存供后续热重试使用。
            if self.config.rush_mode:
                if price_coords is not None:
                    self._cached_hot_path_coords["price"] = price_coords
                if buy_button_coords is not None:
                    self._cached_hot_path_coords["sku_buy"] = buy_button_coords

            # 场次识别和截图补字会拖住下单。探测 / 正式抢票直接点，不再读场次列表。
            if not self._trust_page_defaults():
                self._log_visible_sessions_and_prices(
                    xml_root=_sku_xml if self.config.rush_mode else None
                )

            # 3. 票价选择 - 热路径进入时默认已勾选，直接点确定下单。
            skip_price_selection = getattr(self, "_sale_hot_path", False) or (
                validation_shortcut
                and self.config.rush_mode
                and self._has_element(By.ID, "layout_num")
            )
            if reservation_reached_confirm:
                logger.info("预约页底部按钮已进入确认页，跳过票档点击")
            elif skip_price_selection:
                if getattr(self, "_sale_hot_path", False):
                    logger.info("热路径：票档页已默认勾选，跳过选票直接下单")
                    log_event(
                        logger,
                        "click_strategy",
                        action="select_price",
                        strategy="skip_default_selected",
                    )
                else:
                    logger.info(
                        "开发验证极速路径：检测到已处于可调数量状态，跳过票档点击"
                    )
            else:
                logger.info("选择票价...")
                try:
                    with self._timed_step("select_price"):
                        selected = self._select_price_option(cached_coords=price_coords)
                except SoldOutError as exc:
                    self._save_price_failure_dump(reason=f"sold_out: {exc}")
                    logger.error("票档已售罄，停止本轮抢票: %s", exc)
                    self._log_step_timing_summary()
                    return False
                except PriceSelectorError as exc:
                    self._save_price_failure_dump(reason=str(exc))
                    logger.error("价格选择失败: %s", exc)
                    self._log_step_timing_summary()
                    return False
                if not selected:
                    if getattr(self, "_hot_path_reservation", False):
                        self._hot_path_reservation = False
                        reached = self._hammer_reservation_bottom_button()
                        if reached == "confirm":
                            reservation_reached_confirm = True
                            page_probe["state"] = "order_confirm_page"
                            page_probe["submit_button"] = True
                            selected = True
                    if not selected:
                        self._save_price_failure_dump(reason="select returned False")
                        self._log_step_timing_summary()
                        return False

            # 4. 数量选择
            if not reservation_reached_confirm:
                logger.info("选择数量...")
                if len(self.config.users) > 1:
                    with self._timed_step("adjust_quantity"):
                        self._adjust_quantity_to_user_count()

            # 5. 确定购买 — brief wait for price selection to register.
            # Damai App ignores confirm clicks until btn_buy_view becomes clickable (price > 0).
            # 极速模式确认循环自带 2 连击 + 4s 重试窗口，首击落空有兜底，只需短暂让面板登记票档。
            if reservation_reached_confirm:
                logger.info("预约页底部按钮已进入确认页，继续确认页流程")
                submit_ready = True
                confirm_deadline = time.time()
            else:
                hot = getattr(self, "_sale_hot_path", False)
                if not hot:
                    self._sleep_logged(
                        0.1 if self.config.rush_mode else 0.5,
                        "price_register_before_confirm",
                    )
                logger.info("确定购买...")
                submit_ready = False
                # 正式抢票热路径也要给页面跳转留窗口；0.28s 在慢机/弱网会未进确认页就失败。
                confirm_deadline = time.time() + (
                    1.5 if hot else (4.0 if self.config.rush_mode else 1.8)
                )
            confirm_attempt = 0
            # probe 与 commit 同为 2 连击；只有开发验证捷径降为 1 次。
            burst_count = 1 if validation_shortcut else 2
            confirm_t0 = time.perf_counter()
            while time.time() < confirm_deadline and not submit_ready:
                confirm_attempt += 1
                if getattr(self, "_sale_hot_path", False):
                    # 票档页右侧「确定」：固定相对坐标，不查控件。
                    point = buy_button_coords or self._right_bottom_bar_point()
                    if point is not None:
                        self._burst_click_coordinates(
                            *point,
                            count=burst_count,
                            interval_ms=25,
                            duration=25,
                        )
                        buy_button_coords = point
                        log_event(
                            logger,
                            "click_strategy",
                            action="sku_confirm",
                            strategy="right_bottom_bar_point",
                            attempt=confirm_attempt,
                        )
                    else:
                        self._click_sku_buy_button_element(burst_count=burst_count)
                        log_event(
                            logger,
                            "click_strategy",
                            action="sku_confirm",
                            strategy="btn_buy_element_coords",
                            attempt=confirm_attempt,
                        )
                elif self.config.rush_mode and buy_button_coords:
                    if confirm_attempt == 1:
                        self._burst_click_coordinates(
                            *buy_button_coords,
                            count=burst_count,
                            interval_ms=25,
                            duration=25,
                        )
                        log_event(
                            logger,
                            "click_strategy",
                            action="sku_confirm",
                            strategy="cached_buy_coords",
                            attempt=confirm_attempt,
                        )
                    else:
                        if not self._click_sku_buy_button_element(
                            burst_count=burst_count
                        ):
                            self._burst_click_coordinates(
                                *buy_button_coords,
                                count=burst_count,
                                interval_ms=25,
                                duration=25,
                            )
                            log_event(
                                logger,
                                "click_strategy",
                                action="sku_confirm",
                                strategy="cached_buy_coords_retry",
                                attempt=confirm_attempt,
                            )
                        else:
                            log_event(
                                logger,
                                "click_strategy",
                                action="sku_confirm",
                                strategy="btn_buy_element_coords",
                                attempt=confirm_attempt,
                            )
                elif self.config.rush_mode:
                    # Element click may not work on Damai's custom btn_buy_view —
                    # use coordinate click from XML bounds as primary method.
                    _buy_coords = self._get_buy_button_coordinates()
                    if _buy_coords:
                        self._burst_click_coordinates(
                            *_buy_coords, count=burst_count, interval_ms=25, duration=25
                        )
                        buy_button_coords = _buy_coords  # cache for next retry
                    else:
                        if not self._click_sku_buy_button_element(
                            burst_count=burst_count
                        ):
                            try:
                                buy_button = self._find(
                                    By.ID, "cn.damai:id/btn_buy_view"
                                )
                                self._burst_click_element_center(
                                    buy_button,
                                    count=burst_count,
                                    interval_ms=25,
                                    duration=25,
                                )
                            except Exception:
                                self.ultra_fast_click(By.ID, "cn.damai:id/btn_buy_view")
                else:
                    if not self.ultra_fast_click(By.ID, "cn.damai:id/btn_buy_view"):
                        self.ultra_fast_click(
                            ANDROID_UIAUTOMATOR,
                            'new UiSelector().textMatches(".*确定.*|.*购买.*")',
                        )
                # Short wait then check if confirm page appeared
                remaining = confirm_deadline - time.time()
                check_cap = 0.35 if getattr(self, "_sale_hot_path", False) else 1.0
                check_timeout = min(check_cap, max(0.08, remaining))
                submit_ready = self._wait_for_submit_ready(
                    timeout=check_timeout,
                    poll_interval=0.03 if self.config.rush_mode else 0.05,
                )
                if not submit_ready and confirm_attempt < 5:
                    logger.debug(f"确定按钮第 {confirm_attempt} 次点击未生效，重试...")
                # Every 3rd attempt, check if buy button shows sold-out text.
                if not submit_ready and confirm_attempt % 3 == 0:
                    if self._is_buy_button_sold_out():
                        logger.warning(
                            "购买按钮区域检测到缺货/售罄标识，该票档当前不可购买"
                        )
                        self._set_terminal_failure("sold_out")
                        self._log_step_timing_summary()
                        return False
            confirm_elapsed = time.perf_counter() - confirm_t0
            confirm_ms = int(confirm_elapsed * 1000)
            log_step(
                logger,
                "confirm_purchase",
                duration_ms=confirm_ms,
                attempts=confirm_attempt,
                submit_ready=submit_ready,
            )
            self._last_run_step_timings.append(
                {
                    "step": "confirm_purchase",
                    "seconds": round(confirm_elapsed, 3),
                    "duration_ms": confirm_ms,
                    "manual_baseline_seconds": None,
                    "faster_than_manual": True,
                }
            )
            if not submit_ready:
                if self._trust_page_defaults():
                    logger.info("使用页面默认观演人，不按姓名查找")
                elif validation_shortcut and self.config.rush_mode:
                    logger.info(
                        "开发验证极速路径：确认页未完全就绪，跳过预选用户兜底，直接校验观演人区域"
                    )
                else:
                    # 6. 批量选择用户
                    logger.info("选择用户...")
                    user_clicks = [
                        (ANDROID_UIAUTOMATOR, f'new UiSelector().text("{user}")')
                        for user in self.config.users
                    ]
                    user_timeout = 0.35 if self.config.rush_mode else 1.0
                    clicked_users = self.ultra_batch_click(
                        user_clicks, timeout=user_timeout
                    )
                    if not clicked_users:
                        self._log_visible_attendees()
                    if clicked_users:
                        submit_ready = self._wait_for_submit_ready(
                            timeout=0.9 if self.config.rush_mode else 1.5,
                            poll_interval=0.03 if self.config.rush_mode else 0.05,
                        )

            if not submit_ready and not (
                validation_shortcut and self.config.rush_mode
            ):
                logger.warning("未进入订单确认页，请检查票档可用性或观演人配置")
                self._log_step_timing_summary()
                return False

            submit_selectors = [
                (ANDROID_UIAUTOMATOR, 'new UiSelector().text("立即提交")'),
                (
                    ANDROID_UIAUTOMATOR,
                    'new UiSelector().textMatches(".*提交.*|.*确认.*")',
                ),
                (By.XPATH, '//*[contains(@text,"提交")]'),
            ]
            if self._trust_page_defaults():
                logger.info("使用页面默认观演人，不按姓名勾选")
                attendees_ready = True
            else:
                with self._timed_step("select_attendees"):
                    attendees_ready = self._ensure_attendees_selected_on_confirm_page(
                        require_attendee_section=validation_shortcut
                        and self.config.rush_mode
                    )
                    self._log_visible_attendees()
            if not attendees_ready:
                self._set_terminal_failure("attendee_unselected")
                logger.error("订单提交前观演人未选择完整，已停止自动提交")
                self._log_step_timing_summary()
                return False

            if not self._should_submit_order():
                end_time = time.time()
                if self.config.probe_only:
                    self._set_run_outcome("probe_ready")
                    logger.info(
                        "安全探测：下单流程已走完，停止在“立即提交”前，未提交订单"
                    )
                    logger.info(
                        f"探测完成，未提交订单，耗时: {end_time - start_time:.2f}秒"
                    )
                else:
                    self._set_run_outcome("validation_ready")
                    logger.info(
                        "if_commit_order=False，已完成观演人勾选，停止在“立即提交”前"
                    )
                    logger.info(
                        f"已到订单确认页且观演人已勾选，未提交订单（开发验证），耗时: {end_time - start_time:.2f}秒"
                    )
                self._log_step_timing_summary()
                return True

            # 7. 提交订单
            logger.info("提交订单...")
            with self._timed_step("submit_order"):
                result = self._submit_order_fast(submit_selectors)
            finalized = self._finalize_submit_result(result, start_time=start_time)
            self._log_step_timing_summary()
            return finalized

        except Exception as e:
            logger.error(f"抢票过程发生错误: {e}")
            try:
                from .recovery_strategies import capture_failure_artifacts

                capture_failure_artifacts(self, "run_ticket_grabbing", error=e)
            except Exception:  # pragma: no cover — failure capture must not crash
                logger.debug("failure artifact capture failed", exc_info=True)
            self._log_step_timing_summary()
            return False
        finally:
            time.sleep(0.05)

    # 数量文本的候选 resource-id：不同大麦版本命名不一致（issue #62）。
    _QUANTITY_TEXT_RIDS = frozenset({"text_num", "tv_num", "number"})

    # 票数是个位/十位数；用于把数量文本与价格等大数值区分开。
    _MAX_TICKET_QUANTITY = 20

    def _read_current_quantity(self, xml_root=None):
        """读取 SKU 页数量选择器当前值；读不到返回 None（issue #62）。

        预约抢票的 SKU 页会按预约观演人数预置票数，不能假设初始为 1。
        T-0 每次设备往返都值钱：单次 hierarchy dump 内完成 layout_num 定位、
        候选 id 匹配与纯数字兜底，不做逐候选的 selector 轮询。
        """
        root = xml_root or self._dump_hierarchy_xml()
        if root is None:
            return None
        for node in root.iter("node"):
            if node.get("resource-id", "").endswith("/layout_num"):
                break
        else:
            return None
        subtree = []
        for sub in node.iter("node"):
            value = (sub.get("text") or "").strip()
            if value:
                rid = sub.get("resource-id", "").split("/")[-1]
                subtree.append((rid, value))
        for rid, value in subtree:
            if rid in self._QUANTITY_TEXT_RIDS and value.isdigit():
                return int(value)
        for _, value in subtree:
            # 数量是个位/十位数；排除价格等大数值误读
            if len(value) <= 2 and value.isdigit() and 1 <= int(value) <= self._MAX_TICKET_QUANTITY:
                return int(value)
        return None

    def _click_quantity_button(self, resource_id, clicks):
        """连续点击数量 +/－ 按钮；任一次失败返回 False 由调用方兜底。"""
        try:
            button = self._find(By.ID, resource_id)
            for _ in range(clicks):
                rect = self._element_rect(button)
                x = rect["x"] + rect["width"] // 2
                y = rect["y"] + rect["height"] // 2
                self._click_coordinates(x, y, duration=50)
                time.sleep(0.02)
            return True
        except Exception as e:
            logger.error(f"点击数量按钮 {resource_id} 失败: {e}")
            return False

    def _adjust_quantity_to_user_count(self):
        """把票数对齐到观演人数（issue #62：预约抢票页会预置票数，盲加会超买）。"""
        target = len(self.config.users)
        root = self._dump_hierarchy_xml()
        if root is None:
            logger.warning("无法获取页面结构，跳过数量调整")
            return
        if not any(
            node.get("resource-id", "").endswith("/layout_num")
            for node in root.iter("node")
        ):
            logger.info("面板无数量选择器（选座/缺货登记等流程），跳过数量调整")
            return
        current = self._read_current_quantity(xml_root=root)
        if current is None:
            # 读不到当前票数时绝不盲点（#62 练手复现：面板被预约偏好预置 2 张，
            # 按"默认 1 张"盲加变成 3 张）。数量维持面板现状，对不上由确认页
            # 的人数校验兜底中止。
            logger.error(
                "无法读取当前票数，跳过数量调整（期望 %d 张）；确认页人数不符将中止提交",
                target,
            )
            return
        diff = target - current
        if diff == 0:
            logger.info("当前票数已是 %d 张，无需调整", target)
            return
        if diff > 0:
            self._click_quantity_button("img_jia", diff)
            return
        if not self._click_quantity_button("img_jian", -diff):
            logger.error(
                "当前票数 %d 张超过目标 %d 张且未找到减号按钮，请立即人工核对票数",
                current,
                target,
            )

    def _ensure_performance_date_selected(self, max_wait=3.0):
        """确保目标场次日期被点上（12:20 实战：面板刚展开时日期卡片尚未渲染，
        单次点击落空后直接进票档步骤，整轮作废）。按 0.3s 点击 + 0.1s 间隔
        重试至 max_wait；面板始终无该日期卡片（单场次或配置不符）时放行，
        按默认场次继续。
        """
        if not self.config.date:
            return
        date_selector = f'new UiSelector().textContains("{self.config.date}")'
        attempts = max(1, int(max_wait / 0.4))
        for attempt in range(1, attempts + 1):
            if self.ultra_fast_click(ANDROID_UIAUTOMATOR, date_selector, timeout=0.3):
                logger.info("选择场次日期: %s (attempt=%d)", self.config.date, attempt)
                return
            time.sleep(0.1)
        logger.warning(
            "%.1fs 内未出现日期 '%s' 卡片，按面板默认场次继续",
            attempts * 0.4,
            self.config.date,
        )

    def run_with_retry(self, max_retries=3, initial_page_probe=None):
        """带重试机制的抢票"""
        self._attempts_made = 0
        for attempt in range(max_retries):
            self._attempts_made += 1
            logger.info(f"第 {attempt + 1} 次尝试（{self._execution_mode_label()}）...")
            # Pass initial_page_probe only on the first attempt; retries must re-probe.
            probe = initial_page_probe if attempt == 0 else None
            if self.run_ticket_grabbing(initial_page_probe=probe):
                self._log_success_outcome()
                return True

            if self._terminal_failure_reason:
                logger.error(
                    f"检测到不可重试失败，停止后续重试: {self._terminal_failure_reason}"
                )
                break

            # Fast retry within same session
            for fast_attempt in range(self.config.fast_retry_count):
                self._attempts_made += 1
                logger.info(
                    f"快速重试 {fast_attempt + 1}/{self.config.fast_retry_count}"
                    f"（{self._execution_mode_label()}）..."
                )
                if fast_attempt > 0 and self.config.fast_retry_interval_ms > 0:
                    time.sleep(self.config.fast_retry_interval_ms / 1000)
                if self._fast_retry_from_current_state():
                    self._log_success_outcome("快速重试成功：")
                    return True
                if self._terminal_failure_reason:
                    logger.error(
                        f"快速重试遇到不可重试失败，停止后续重试: {self._terminal_failure_reason}"
                    )
                    break

            if self._terminal_failure_reason:
                break

            # Full driver recreation
            logger.warning(f"第 {attempt + 1} 次尝试及快速重试均失败")
            if attempt < max_retries - 1:
                if not self.config.auto_navigate:
                    logger.info("手动起跑模式，保留当前会话并继续本地重试")
                    continue
                logger.info("重建驱动后重试...")
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self._setup_driver()

        logger.error("所有尝试均失败")
        return False
