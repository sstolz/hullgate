"""Slice 5 real-server browser gates, including SCR-11/12 dark/light captures. Not run locally."""

from browser_support import CONSOLE, Watched, host
from console_support import DEV_IDLE, accessibility, both_themes, focus_visible, seed, tab_to


def test_ac_21_undo_confirm_stale_and_keyboard(operator: Watched) -> None:
    seed()
    host("settings", "set", DEV_IDLE, "token_duration", "7d")
    page = operator.page
    page.goto(f"{CONSOLE}/admin/audit")
    page.get_by_label("Action prefix", exact=True).fill("settings.set")
    page.get_by_label("Path prefix", exact=True).fill(DEV_IDLE)
    page.get_by_role("button", name="Filter", exact=True).click()
    page.get_by_role("button", name="Undo", exact=True).first.click()
    page.locator("[data-state='SCR-11.undo-confirm']").wait_for()
    both_themes(operator, "SCR-11.undo-confirm")
    accessibility(operator, "SCR-11.undo-confirm")
    host("settings", "set", DEV_IDLE, "token_duration", "14d")
    page.get_by_role("button", name="Confirm undo", exact=True).click()
    page.locator("[data-state='SCR-11.undo-stale']").wait_for()
    both_themes(operator, "SCR-11.undo-stale")
    assert page.get_by_role("button", name="Confirm undo", exact=True).is_disabled()
    page.keyboard.press("Escape")
    page.get_by_role("dialog").wait_for(state="detached")
    # The newest row (the 14d change) only exists once the filtered list is read again.
    with page.expect_response(lambda r: "/admin/api/v1/audit" in r.url):
        page.get_by_role("button", name="Filter", exact=True).click()
    page.get_by_role("button", name="Undo", exact=True).first.click()
    page.locator("[data-state='SCR-11.undo-confirm']").wait_for()
    # The dialog focuses Confirm after a mouse click, which shows no focus ring (no keyboard
    # modality yet): a keyboard user tabs at least once, then reaches Confirm inside the trap.
    page.keyboard.press("Tab")
    tab_to(operator, "e.textContent.trim() === 'Confirm undo'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.get_by_role("dialog").wait_for(state="detached")
    assert "7d" in next(
        line for line in host("settings", "get", DEV_IDLE).splitlines() if "token_duration" in line
    )


def test_ac_21_import_states_export_and_apply(operator: Watched) -> None:
    seed()
    page = operator.page
    page.goto(f"{CONSOLE}/admin/import")
    page.get_by_label("YAML document", exact=True).wait_for()
    both_themes(operator, "SCR-12.upload")
    text = host("export")
    page.get_by_label("YAML file", exact=True).set_input_files(
        {"name": "hullgate.yaml", "mimeType": "application/yaml", "buffer": text.encode()}
    )
    page.get_by_role("button", name="Preview changes", exact=True).click()
    page.locator("[data-state='SCR-12.preview-empty']").wait_for()
    both_themes(operator, "SCR-12.preview-empty")
    accessibility(operator, "SCR-12.preview-empty")
    page.get_by_label("YAML document", exact=True).fill("!!python/object:os.system {}")
    page.get_by_role("button", name="Preview changes", exact=True).click()
    page.locator("[data-state='SCR-12.refused']").wait_for()
    both_themes(operator, "SCR-12.refused")
    # Read a real document, then change state in another operator session.
    page.get_by_label("YAML document", exact=True).fill(
        text.replace('name: "Organisation"', 'name: "Imported browser organisation"', 1)
    )
    host("settings", "set", DEV_IDLE, "token_duration", "14d")
    page.get_by_role("button", name="Preview changes", exact=True).click()
    page.get_by_role("button", name="Apply import", exact=True).wait_for()
    assert not page.get_by_label("Apply the listed deletions", exact=True).is_checked()
    both_themes(operator, "SCR-12")
    host("settings", "set", DEV_IDLE, "token_duration", "3d")
    page.get_by_role("button", name="Apply import", exact=True).click()
    page.locator("[data-state='SCR-12.stale']").wait_for()
    both_themes(operator, "SCR-12.stale")
    page.get_by_role("button", name="Preview changes", exact=True).click()
    with page.expect_download() as download:
        page.get_by_role("button", name="Download current export", exact=True).last.click()
    assert download.value.suggested_filename == "hullgate.yaml"
    tab_to(operator, "e.textContent.trim() === 'Apply import'")
    assert focus_visible(operator)
    page.keyboard.press("Enter")
    page.get_by_role("status").filter(has_text="Import applied.").wait_for()
