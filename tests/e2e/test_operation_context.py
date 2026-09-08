"""Browser regressions for deliberate review while collaboration changes around it."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright

from . import test_web_client as client

_enter_demo_workspace = client._enter_demo_workspace
_require_chromium = client._require_chromium
live_server = client.live_server


def test_output_review_keeps_focus_and_disclosure_during_snapshot(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        page.locator("button.branch-nav").first.click()
        page.locator(".output-card details").first.locator("summary").click()
        button = page.locator(".output-card button.exclude").first
        button.focus()
        page.evaluate("() => { window.reviewButton = document.activeElement; }")
        page.evaluate("() => window.loadState()")
        assert page.evaluate("() => document.activeElement === window.reviewButton")
        assert page.locator(".output-card details").first.get_attribute("open") is not None
        browser.close()


def test_branch_switch_keeps_each_synthesis_title_with_its_branch(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        values = page.evaluate("""async () => {
          const {state} = await import('/static/js/state.js');
          const {updateSelectionSummary} = await import('/static/js/branch.js');
          const original = state.currentBranchId;
          const input = document.querySelector('#synthesis-title');
          input.value = 'Human-authored authentication decision';
          input.dispatchEvent(new Event('input', {bubbles: true}));
          state.roomBranches.push({branch_id:'other-branch', initiating_prompt:'Choose a cache.'});
          state.currentBranchId = 'other-branch';
          updateSelectionSummary();
          const other = input.value;
          input.value = 'Cache rollout';
          input.dispatchEvent(new Event('input', {bubbles: true}));
          state.currentBranchId = original;
          updateSelectionSummary();
          return {other, original: input.value};
        }""")
        assert values == {
            "other": "Choose a cache",
            "original": "Human-authored authentication decision",
        }
        browser.close()


def test_branch_cards_keep_review_counts_when_another_branch_is_selected(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        original = page.evaluate("""async () => {
          const {state} = await import('/static/js/state.js');
          const {api} = await import('/static/js/api.js');
          const {selectBranch} = await import('/static/js/branch.js');
          const original = state.currentBranchId;
          const result = await api('POST', `/rooms/${state.roomId}/branches`, {
            mode: 'TURN_LOCKED_SINGLE', prompt: 'Choose a cache.',
            agent_ids: [state.roomAgents[0].agent_id]
          });
          await window.loadState();
          await selectBranch(result.branch.branch_id);
          if (state.currentBranchId !== result.branch.branch_id) {
            throw new Error('Branch switch did not settle');
          }
          return original;
        }""")
        card = page.locator(f'.branch-activity [data-branch-id="{original}"]')
        assert "2 included" in card.text_content()
        browser.close()


def test_artifact_evidence_does_not_depend_on_selected_branch(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        text = page.evaluate("""async () => {
          const {state} = await import('/static/js/state.js');
          const {renderOntology} = await import('/static/js/ontology.js');
          state.roomOutputs = [];
          renderOntology(state.roomOntology);
          return document.querySelector('#ontology-tree').textContent;
        }""")
        assert "provider snapshot unavailable" not in text
        assert "Source prompt: Unavailable" not in text
        browser.close()


def test_launch_stays_in_its_original_channel_after_navigation(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        rooms = page.evaluate("""async () => {
          const {state} = await import('/static/js/state.js');
          const {api} = await import('/static/js/api.js');
          const room = await api('POST', `/workspaces/${state.workspaceId}/rooms`, {
            name:'Cache planning'
          });
          await window.refreshRooms();
          return {original: state.roomId, other: room.room_id};
        }""")
        held = []
        branch_urls = []
        page.route("**/api/v1/rooms/*/agents", lambda route: held.append(route))
        page.on(
            "request",
            lambda request: (
                branch_urls.append(request.url)
                if request.method == "POST" and request.url.endswith("/branches")
                else None
            ),
        )
        page.locator("#ai-trigger").click()
        page.locator("#strategy-single").click()
        page.locator("#analysis-question").fill("Review authentication options")
        page.locator("#launch-button").click()
        assert held
        page.evaluate("room => window.switchRoom(room)", rooms["other"])
        for route in held:
            route.fulfill(response=route.fetch())
        page.wait_for_function(
            "() => !document.querySelector('#launch-button').hasAttribute('aria-busy')"
        )
        assert len(branch_urls) == 1
        assert f"/rooms/{rooms['original']}/branches" in branch_urls[0]
        assert page.evaluate("() => window.roomId") == rooms["other"]
        browser.close()


def test_meta_does_not_repaint_an_answer_from_a_channel_that_was_left(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        other_room = page.evaluate("""async () => {
          const {state} = await import('/static/js/state.js');
          const {api} = await import('/static/js/api.js');
          const room = await api('POST', `/workspaces/${state.workspaceId}/rooms`, {
            name:'Meta isolation'
          });
          await window.refreshRooms();
          return room.room_id;
        }""")
        held = []
        page.route("**/api/v1/rooms/*/meta?**", lambda route: held.append(route))
        page.evaluate("""async () => {
          const {renderMeta} = await import('/static/js/meta.js');
          window.pendingMeta = renderMeta('kind=status');
        }""")
        page.wait_for_function(
            "() => document.querySelector('#meta-answer').textContent.includes('Retrieving')"
        )
        assert held
        response = held[0].fetch()
        page.evaluate("room => window.switchRoom(room)", other_room)
        held[0].fulfill(response=response)
        page.evaluate("() => window.pendingMeta")
        assert "Ask about this channel" in page.locator("#meta-answer").text_content()
        assert not page.locator("#meta-evidence").text_content().strip()
        browser.close()


def test_notification_can_be_opened_with_the_keyboard(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        room_id = page.evaluate("() => window.roomId")
        page.route(
            "**/api/v1/notifications",
            lambda route: route.fulfill(
                json=[
                    {
                        "room_id": room_id,
                        "title": "Review requested",
                        "body": "Check the architecture output",
                        "created_at": "2026-09-08T12:00:00Z",
                    }
                ]
            ),
        )
        page.locator('.room-header [data-action="openNotifications"]').click()
        notification = page.get_by_role("button", name="Review requested")
        notification.focus()
        notification.press("Enter")
        assert not page.locator(".right-panel").evaluate("el => el.classList.contains('open')")
        browser.close()


def test_publication_is_single_flight_and_opens_the_artifact_it_created(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        page.locator("button.branch-nav").first.click()
        page.locator("#synthesis-title").fill("Reviewed authentication recommendation")
        # The seeded artifact is a Decision Brief. Publish a different type so
        # retaining the previously selected artifact cannot pass accidentally.
        page.locator("#synthesis-type").select_option("GENERAL_SYNTHESIS")
        held = []
        page.route("**/api/v1/branches/*/syntheses", lambda route: held.append(route))
        page.evaluate("""async () => {
          const {publishSynthesis} = await import('/static/js/branch.js');
          window.pendingPublication = publishSynthesis();
          void publishSynthesis();
        }""")
        page.wait_for_function("() => document.querySelector('#synthesize-button').disabled")
        assert len(held) == 1
        assert "Publishing" in page.locator("#synthesis-guidance").text_content()
        assert page.locator("#synthesize-button").get_attribute("aria-busy") == "true"
        page.evaluate("() => window.loadState()")
        assert page.locator("#synthesize-button").is_disabled()
        response = held[0].fetch()
        assert response.ok, response.text()
        result = response.json()
        held[0].fulfill(response=response)
        page.evaluate("() => window.pendingPublication")
        artifact_id = page.evaluate("""async () => {
          const {state} = await import('/static/js/state.js');
          return state.selectedArtifactId;
        }""")
        assert artifact_id == result["artifact_id"]
        browser.close()


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_header_targets_are_reachable_without_losing_channel_name(live_server, width):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 844}, has_touch=True)
        _enter_demo_workspace(page, live_server.base_url)
        name_width = page.evaluate("""async () => {
          const {state} = await import('/static/js/state.js');
          const {updateRoomHeader} = await import('/static/js/shell.js');
          state.currentRoomName = 'Infrastructure decisions';
          updateRoomHeader();
          return document.querySelector('#room-name').getBoundingClientRect().width;
        }""")
        targets = page.locator(".room-header button").evaluate_all("""els => els
          .filter(el => el.getBoundingClientRect().width > 0)
          .map(el => ({label:el.getAttribute('aria-label'),
                      ...el.getBoundingClientRect().toJSON()}))""")
        assert all(t["width"] >= 44 and t["height"] >= 44 for t in targets), targets
        assert all(t["x"] >= 0 and t["right"] <= width for t in targets), targets
        assert name_width >= 70
        browser.close()


@pytest.mark.parametrize("width, first_item", [(1440, "Invite people"), (390, "Mark channel read")])
def test_channel_menu_keyboard_navigation_skips_hidden_items(live_server, width, first_item):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 844})
        _enter_demo_workspace(page, live_server.base_url)
        trigger = page.locator("#channel-menu-button")
        trigger.focus()
        trigger.press("Enter")
        assert page.evaluate("() => document.activeElement.textContent.trim()") == first_item
        page.keyboard.press("ArrowUp")
        assert page.evaluate("() => document.activeElement.textContent.trim()") == "Leave channel"
        page.keyboard.press("ArrowDown")
        assert page.evaluate("() => document.activeElement.textContent.trim()") == first_item
        page.keyboard.press("Escape")
        assert trigger.evaluate("el => el === document.activeElement")
        browser.close()
