"""Visible browser interactions only; browser-managed authentication stays local."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from . import MODEL_LABEL, SOURCE


class BridgeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def conversation_url(value):
    parsed = urlsplit(str(value))
    if parsed.scheme != 'https' or parsed.hostname != 'chatgpt.com' or parsed.username or parsed.password:
        return None
    return 'https://chatgpt.com' + parsed.path if re.fullmatch(r'/c/[A-Za-z0-9-]+', parsed.path) else None


class ChatBrowser:
    def __init__(self, root: Path, channel='chrome', poll_seconds=1.0):
        self.root, self.channel, self.poll_seconds = Path(root), channel, poll_seconds
        self.playwright = self.context = self.page = None

    async def open(self):
        if self.context is not None:
            try:
                if self.page and not self.page.is_closed():
                    return await self.inspect(self.page)
            except Exception:
                pass
            await self.close()
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise BridgeError('playwright_not_installed') from None
        self.root.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str((self.root / 'browser-profile').resolve()), channel=self.channel,
                headless=False, chromium_sandbox=True, accept_downloads=False,
                viewport={'width': 1280, 'height': 900})
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto('https://chatgpt.com', wait_until='domcontentloaded', timeout=60000)
        except Exception:
            await self.close()
            raise BridgeError('browser_open_failed') from None
        return await self.inspect(self.page)

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self.playwright:
                await self.playwright.stop()
            self.context = self.playwright = self.page = None

    @staticmethod
    async def visible(locator):
        for i in range(await locator.count()):
            entry = locator.nth(i)
            if await entry.is_visible():
                return entry
        return None

    async def composer(self, page):
        return await self.visible(page.locator('#prompt-textarea, #mobile-composer-prompt, textarea[aria-label="Message ChatGPT"], textarea[aria-label="向 ChatGPT 发送消息"]'))

    async def picker(self, page):
        return await self.visible(page.locator('[data-testid="model-switcher-dropdown-button"]')) or await self.visible(
            page.get_by_role('button', name=re.compile(r'^(ChatGPT|GPT[- ]?6|Pro)(\s|$)|模型选择|选择模型', re.I)))

    async def inspect(self, page=None):
        page = page or self.page
        state = {'source': SOURCE, 'product': 'Chat', 'model_verified': False,
                 'model_label': None, 'reasoning_effort': None, 'usage_reported': False,
                 'status': 'browser_closed', 'ok': False}
        if page is None or page.is_closed():
            return state
        parsed = urlsplit(page.url)
        if parsed.hostname != 'chatgpt.com':
            state['status'] = 'login_required' if parsed.hostname in {'auth.openai.com', 'auth0.openai.com'} else 'unexpected_page'
            return state
        # Inspect rendered text only; never inspect storage, cookies, or network payloads.
        body = await page.locator('body').inner_text(timeout=5000)
        if re.search(r'Verify you are human|Checking your browser|验证您是人类|验证你是真人|Just a moment', body, re.I):
            state['status'] = 'human_verification_required'
            return state
        login = await self.visible(page.get_by_role('button', name=re.compile(r'^(Log in|Sign in|登录)$', re.I)))
        if login:
            state['status'] = 'login_required'
            return state
        if re.search(r'/codex(?:/|$)|/work(?:/|$)', parsed.path):
            state['status'] = 'chat_mode_required'
            return state
        work = await self.visible(page.locator('[role="tab"][aria-selected="true"]'))
        if work and re.search(r'\bWork\b|工作', await work.inner_text(), re.I):
            state['status'] = 'chat_mode_required'
            return state
        if await self.composer(page) is None:
            state['status'] = 'composer_not_found'
            return state
        picker = await self.picker(page)
        if picker:
            label = ((await picker.inner_text()) + ' ' + (await picker.get_attribute('aria-label') or '')).strip()
            state['model_label'] = label[:160]
            state['model_verified'] = bool(re.search(r'\bGPT[- ]?6\s+Pro\b', label, re.I))
        state['status'] = 'ready' if state['model_verified'] else 'model_selection_required'
        state['ok'] = state['model_verified']
        return state

    async def ensure_model(self, page):
        state = await self.inspect(page)
        if state['ok']:
            return state
        if state['status'] != 'model_selection_required':
            raise BridgeError(state['status'])
        picker = await self.picker(page)
        if picker is None:
            raise BridgeError('model_picker_not_found')
        await picker.click()
        option = await self.visible(page.get_by_role('menuitem', name=re.compile(r'GPT[- ]?6\s+Pro', re.I)))
        option = option or await self.visible(page.get_by_role('menuitemradio', name=re.compile(r'GPT[- ]?6\s+Pro', re.I)))
        if option is None:
            await page.keyboard.press('Escape')
            raise BridgeError('requested_model_not_visible')
        observed = (await option.inner_text()).strip()[:300]
        await option.click()
        state = await self.inspect(page)
        if not state['ok']:
            # A compact "Pro" button is acceptable only after an exact GPT-6 Pro
            # option was observed and selected in this same page interaction.
            compact = re.fullmatch(r'(?:ChatGPT\s+)?Pro', state['model_label'] or '', re.I)
            if compact:
                state.update(ok=True, status='ready', model_verified=True)
            else:
                raise BridgeError('model_selection_unverified')
        state['selected_option_text'] = observed
        return state

    async def prepare(self, prior_url=None):
        await self.open()
        page = await self.context.new_page()
        self.page = page
        target = conversation_url(prior_url) if prior_url else 'https://chatgpt.com/'
        if not target:
            raise BridgeError('invalid_conversation_url')
        await page.goto(target, wait_until='domcontentloaded', timeout=60000)
        # Wait for the rendered composer/login rather than treating hydration as auth failure.
        for _ in range(30):
            state = await self.inspect(page)
            if state['status'] != 'composer_not_found':
                break
            await asyncio.sleep(.5)
        evidence = await self.ensure_model(page)
        if not prior_url and await page.locator('[data-message-author-role="user"]').count():
            raise BridgeError('new_chat_not_empty')
        stop = await self.visible(page.get_by_role('button', name=re.compile(r'Stop (generating|streaming)|停止生成|停止流式', re.I)))
        if stop:
            raise BridgeError('conversation_still_generating')
        return page, evidence

    async def fill(self, page, prompt):
        editor = await self.composer(page)
        if editor is None:
            raise BridgeError('composer_not_found')
        await editor.fill(prompt, timeout=15000)

    async def click_send(self, page):
        button = await self.visible(page.locator('[data-testid="send-button"]')) or await self.visible(
            page.get_by_role('button', name=re.compile(r'^(Send( message| prompt)?|发送(消息)?)$', re.I)))
        if button is None or not await button.is_enabled():
            raise BridgeError('send_button_unavailable')
        await button.click(timeout=10000)

    async def read_answer(self, page, marker):
        """Only the assistant following this request's rendered user bubble."""
        messages = page.locator('[data-message-author-role]')
        found = False
        for i in range(await messages.count()):
            entry = messages.nth(i)
            role = await entry.get_attribute('data-message-author-role')
            if role == 'user':
                text = await entry.inner_text()
                if marker in text:
                    found = True
                elif found:
                    raise BridgeError('conversation_modified')
            elif role == 'assistant' and found:
                text = (await entry.inner_text()).strip()
                # Final response actions must exist; idle text alone is not completion.
                turn = entry.locator('xpath=ancestor::*[self::article or @data-testid][1]')
                scope = turn if await turn.count() else entry
                copy_button = await self.visible(scope.get_by_role('button', name=re.compile(r'^(Copy|Copy response|复制|复制回复)$', re.I)))
                stop = await self.visible(page.locator('[data-testid="stop-button"]')) or await self.visible(
                    page.get_by_role('button', name=re.compile(r'Stop (generating|streaming)|停止生成|停止流式', re.I)))
                return {'text': text, 'finished': bool(text and copy_button and not stop)}
        return {'text': '', 'finished': False}

    async def wait_answer(self, page, marker, timeout, on_url):
        deadline, last, stable = time.monotonic() + timeout, None, 0
        while time.monotonic() < deadline:
            url = conversation_url(page.url)
            if url:
                on_url(url)
            state = await self.inspect(page)
            if state['status'] in {'login_required', 'human_verification_required', 'unexpected_page', 'chat_mode_required'}:
                raise BridgeError(state['status'])
            answer = await self.read_answer(page, marker)
            text = answer['text']
            stable = stable + 1 if answer['finished'] and text == last else 0
            last = text
            if stable >= 2:
                return text
            await asyncio.sleep(self.poll_seconds)
        raise BridgeError('generation_timeout_result_unknown')

    async def recover(self, url, marker, timeout, on_url):
        target = conversation_url(url)
        if not target:
            raise BridgeError('conversation_url_not_recorded')
        await self.open()
        page = await self.context.new_page()
        self.page = page
        await page.goto(target, wait_until='domcontentloaded', timeout=60000)
        return await self.wait_answer(page, marker, timeout, on_url)
