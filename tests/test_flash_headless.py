"""Tests for flash_scripts/_headless.py -- picking firmware by COMPONENT name.

dfu.py chooses firmware by prompting; ODIN chooses it by naming components on
smashclicker's command line (`-h pmr -u pmr,dir`). Those names are exactly
FirmwareEntry.component, so the join needs no translation table -- these tests
pin that, plus the two ways a request can be unsatisfiable (no firmware for this
ECU's identity, no validated flash sequence), which must be REPORTED rather than
skipped: a flasher that silently does nothing is the failure this path removes.

Self-contained: a synthetic signed_metadata_map.tsv and a fake session, so
nothing here touches a bus or a firmware artifact.
"""
from pathlib import Path

import pytest

import flash_scripts
from flash_scripts._headless import (
    QuietDisplay,
    bootloader_choice,
    condition_choices,
    ramapp_choice,
    select_entries,
    uds_node_for,
)
from uds_local.metadata import FirmwareEntry


def _entry(component, lookup="pmr:123", src=None, conditions=None, crc="0"):
    return FirmwareEntry(lookup_key=lookup,
                         src_path=src or f"{component}/{component}.bhx",
                         dest_name=f"{component}.bhx", component=component,
                         crc=crc, conditions=conditions or {}, signature="")


class TestSelectEntries:
    def test_components_select_their_entries_in_the_order_asked_for(self):
        entries = [_entry("dir"), _entry("pmr"), _entry("pmrbl")]
        selected, unplaced, ambiguous = select_entries(entries, ["pmrbl", "pmr", "dir"])
        assert [e.component for e in selected] == ["pmrbl", "pmr", "dir"]
        assert unplaced == [] and ambiguous == []

    def test_a_component_with_no_entry_is_reported_not_dropped(self):
        selected, unplaced, ambiguous = select_entries([_entry("pmr")], ["pmr", "dif"])
        assert [e.component for e in selected] == ["pmr"]
        assert unplaced == ["dif"] and ambiguous == []

    def test_matching_is_case_insensitive(self):
        selected, unplaced, _ = select_entries([_entry("pmr")], ["PMR"])
        assert len(selected) == 1 and unplaced == []

    def test_condition_rows_naming_one_image_flash_it_ONCE(self):
        # The real pmr:440467458 has two `pmr` rows differing only in vdcType,
        # both pointing at the same .bhx. Taking both flashed the ECU twice in a
        # row and reported it twice -- and a flash burns a FLASH_COUNT limit.
        rows = [_entry("pmr", conditions={"vdcType": "0"}),
                _entry("pmr", conditions={"vdcType": "1"})]
        selected, unplaced, ambiguous = select_entries(rows, ["pmr"])
        assert len(selected) == 1
        assert unplaced == [] and ambiguous == []

    def test_rows_naming_different_images_are_a_choice_we_do_not_make(self):
        rows = [_entry("pmr", src="a/pmr_a.bhx", crc="aa"),
                _entry("pmr", src="b/pmr_b.bhx", crc="bb")]
        selected, unplaced, ambiguous = select_entries(rows, ["pmr"])
        assert selected == [] and ambiguous == ["pmr"]


class TestConditionChoices:
    """What the operator is actually asked. The map has nineteen condition keys,
    but for one ECU's identity only a couple ever change which image applies --
    asking about the rest would be noise, and asking with raw numbers would make
    the operator decode 'drivetrainType=0' themselves."""

    ROWS = [_entry("pmr", conditions={"chassisType": "2", "drivetrainType": "0",
                                      "vdcType": "0"}),
            _entry("pmr", conditions={"chassisType": "2", "drivetrainType": "0",
                                      "vdcType": "1"})]

    def test_only_the_keys_that_vary_are_offered(self):
        got = condition_choices(self.ROWS, ["pmr"])
        assert [c["key"] for c in got] == ["vdcType"]      # not chassis/drivetrain
        assert [o["value"] for o in got[0]["options"]] == ["0", "1"]

    def test_values_carry_the_firmwares_own_labels(self):
        rows = [_entry("esp", conditions={"drivetrainType": "0"}),
                _entry("esp", conditions={"drivetrainType": "1"})]
        got = condition_choices(rows, ["esp"],
                                label_map={"drivetrainType": {"0": "RWD", "1": "AWD"}})
        assert [(o["value"], o["label"]) for o in got[0]["options"]] == [
            ("0", "RWD"), ("1", "AWD")]

    def test_a_key_with_no_label_table_falls_back_to_its_value(self):
        got = condition_choices(self.ROWS, ["pmr"], label_map={})
        assert [o["label"] for o in got[0]["options"]] == ["0", "1"]

    def test_a_label_table_matches_the_key_across_case(self):
        # The metadata writes brakeHWType on most rows and brakeHwType on the
        # ESP's; the signal naming the values is GTW_brakeHWType.
        rows = [_entry("esp", conditions={"brakeHwType": "0"}),
                _entry("esp", conditions={"brakeHwType": "1"})]
        got = condition_choices(rows, ["esp"],
                                label_map={"brakeHWType": {"0": "BREMBO", "1": "MANDO"}})
        assert [o["label"] for o in got[0]["options"]] == ["BREMBO", "MANDO"]

    def test_the_current_selection_is_reported_back(self):
        got = condition_choices(self.ROWS, ["pmr"], {"vdcType": "1"})
        assert got[0]["value"] == "1"

    def test_rows_for_other_components_do_not_add_choices(self):
        rows = [*self.ROWS, _entry("esp", conditions={"espValveType": "3"}),
                _entry("esp", conditions={"espValveType": "4"})]
        assert [c["key"] for c in condition_choices(rows, ["pmr"])] == ["vdcType"]

    def test_one_matching_row_asks_nothing(self):
        assert condition_choices([self.ROWS[0]], ["pmr"]) == []


class TestBootloaderChoice:
    """dfu.py asks this at the terminal and defaults to NO. Driven from ODIN the
    default runs the other way: an operator who ran UPDATE_VCSEC-WITH-BOOTLOADER
    named the bu/bl by hand, and quietly turning that into a plain app update is
    the silent no-op this whole path exists to remove. The dialog is where it is
    confirmed instead."""

    WITH_BL = [_entry("vcsecbu"), _entry("vcsecbl"), _entry("vcsec")]

    def test_a_flash_with_no_bootloader_asks_nothing(self):
        got = bootloader_choice([_entry("pmr"), _entry("dir")], include=True)
        assert got["available"] == [] and got["note"] is None

    def test_each_bootloader_is_named_with_its_role(self):
        got = bootloader_choice(self.WITH_BL, include=True)
        assert [(e["component"], e["kind"]) for e in got["available"]] == [
            ("vcsecbu", "updater"), ("vcsecbl", "image")]
        assert got["available"][0]["parent"] == "vcsec"

    def test_an_app_that_merely_ends_in_bl_is_not_a_bootloader(self):
        # epbl is a real app; only the recognised bootloader set counts.
        assert bootloader_choice([_entry("epbl")], include=True)["available"] == []

    def test_the_note_says_what_reflashes_afterwards(self):
        note = bootloader_choice(self.WITH_BL, include=True)["note"]
        assert "brick" in note
        assert "vcsec.bhx" in note        # the app that restores normal operation

    def test_the_note_warns_when_nothing_reflashes_after(self):
        # bu+bl with no app leaves the ECU booting the update agent.
        note = bootloader_choice(self.WITH_BL[:2], include=True)["note"]
        assert "no app entry follows" in note

    def test_the_note_reads_correctly_with_several_restoring_apps(self):
        # Real VCSEC has two (vcsec + vcsecramapp), which a "<list> reflashes
        # after it" phrasing gets wrong.
        note = bootloader_choice([*self.WITH_BL, _entry("vcsecramapp")],
                                 include=True)["note"]
        assert "these restore it: vcsec.bhx, vcsecramapp.bhx" in note


class TestExcludingBootloaders:
    ROWS = [("vcsec:123", "a/vcsecbu.bhx", "vcsecbu.bhx", "vcsecbu"),
            ("vcsec:123", "b/vcsecbl.bhx", "vcsecbl.bhx", "vcsecbl"),
            ("vcsec:123", "c/vcsec.bhx", "vcsec.bhx", "vcsec")]

    def _run(self, tmp_path, monkeypatch, **kw):
        import uds_local.identity as identity

        class _Ident:
            packed_key = 123
            lookup_key = "vcsec:123"

        monkeypatch.setattr(identity, "parse_f180", lambda *a, **k: _Ident())
        monkeypatch.setattr("uds_local.identity.parse_f180", lambda *a, **k: _Ident())
        return flash_scripts.flash_components(
            _Session(b"\x00"), _bundle_tsv(tmp_path, self.ROWS), "vcsec",
            ["vcsecbu", "vcsecbl", "vcsec"], dry_run=True, **kw)

    def test_bootloaders_are_written_by_default(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, monkeypatch)
        assert res["flashed"] == ["vcsecbu", "vcsecbl", "vcsec"]
        assert res["excluded"] == []

    def test_excluding_them_leaves_only_the_app(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, monkeypatch, include_bootloaders=False)
        assert res["flashed"] == ["vcsec"]
        assert res["excluded"] == ["vcsecbu", "vcsecbl"]
        assert [r["component"] for r in res["plan"]] == ["vcsec"]

    def test_an_exclusion_is_a_narrowing_not_a_failure(self, tmp_path, monkeypatch):
        # unplaced/ambiguous/no_script all mean "could not", and block the write.
        # This one means "chose not to", so the flash still goes ahead.
        res = self._run(tmp_path, monkeypatch, include_bootloaders=False)
        assert res["ok"] is True
        assert res["unplaced"] == [] and res["ambiguous"] == [] and res["no_script"] == []

    def test_the_choice_is_reported_either_way(self, tmp_path, monkeypatch):
        on = self._run(tmp_path, monkeypatch)["bootloaders"]
        off = self._run(tmp_path, monkeypatch, include_bootloaders=False)["bootloaders"]
        assert on["included"] is True and off["included"] is False
        # The offer survives being declined -- otherwise the checkbox would
        # vanish the moment it was unticked and could never be re-ticked.
        assert [e["component"] for e in off["available"]] == ["vcsecbu", "vcsecbl"]


class TestRamappChoice:
    """A RAM app is not part of a normal app update, so 'just the RAM app,
    without rewriting the app it rides on' is a real bench request -- three
    states, not a checkbox. dfu.py offers the same three."""

    WITH_RAM = [_entry("vcsec"), _entry("vcsecramapp")]

    def test_a_flash_with_no_ramapp_asks_nothing(self):
        assert ramapp_choice([_entry("pmr"), _entry("dir")])["available"] == []

    def test_the_ramapps_are_named(self):
        got = ramapp_choice(self.WITH_RAM)
        assert [e["component"] for e in got["available"]] == ["vcsecramapp"]

    def test_all_three_states_are_offered_when_there_is_an_app_too(self):
        got = ramapp_choice(self.WITH_RAM)
        assert [o["value"] for o in got["options"]] == ["include", "skip", "only"]

    def test_only_is_withheld_when_there_is_nothing_else_to_leave_out(self):
        # With nothing but RAM apps selected, 'include' and 'only' are the same
        # list, so offering both is a choice about nothing.
        got = ramapp_choice([_entry("vcsecramapp")])
        assert [o["value"] for o in got["options"]] == ["include", "skip"]

    def test_an_unknown_mode_falls_back_to_include(self):
        assert ramapp_choice(self.WITH_RAM, "nonsense")["mode"] == "include"

    def test_only_is_not_accepted_when_it_was_not_offered(self):
        assert ramapp_choice([_entry("vcsecramapp")], "only")["mode"] == "include"

    def test_a_pm_ramapp_carries_the_opc_caveat(self):
        # Pushing pmramapp to service an OPC is a wasted step on gen26: the
        # CAN<->LIN gateway is already resident in the PMR app.
        note = ramapp_choice([_entry("pmr"), _entry("pmramapp")])["note"]
        assert "opc/opcs" in note and "resident" in note

    def test_another_ecus_ramapp_does_not(self):
        assert "opc/opcs" not in ramapp_choice(self.WITH_RAM)["note"]


class TestRamappModes:
    ROWS = [("vcsec:123", "a/vcsec.bhx", "vcsec.bhx", "vcsec"),
            ("vcsec:123", "b/vcsecramapp.bhx", "vcsecramapp.bhx", "vcsecramapp")]

    def _run(self, tmp_path, monkeypatch, **kw):
        import uds_local.identity as identity

        class _Ident:
            packed_key = 123
            lookup_key = "vcsec:123"

        monkeypatch.setattr(identity, "parse_f180", lambda *a, **k: _Ident())
        monkeypatch.setattr("uds_local.identity.parse_f180", lambda *a, **k: _Ident())
        return flash_scripts.flash_components(
            _Session(b"\x00"), _bundle_tsv(tmp_path, self.ROWS), "vcsec",
            ["vcsec", "vcsecramapp"], dry_run=True, **kw)

    def test_include_writes_what_the_procedure_names(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, monkeypatch)
        assert res["flashed"] == ["vcsec", "vcsecramapp"]
        assert res["excluded"] == []

    def test_skip_drops_the_ramapp(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, monkeypatch, ramapps="skip")
        assert res["flashed"] == ["vcsec"]
        assert res["excluded"] == ["vcsecramapp"]

    def test_only_drops_everything_else(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, monkeypatch, ramapps="only")
        assert res["flashed"] == ["vcsecramapp"]
        assert res["excluded"] == ["vcsec"]

    def test_a_narrowed_flash_still_goes_ahead(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, ramapps="only")["ok"] is True


class TestDualCpuNarration:
    """UPDATE_PMR flashes ['pmr','dir'] -- which IS the dual-CPU pair (pmr is a
    PCS-family primary, dir a secondary), so the whole flash goes through
    run_pcs_dual_cpu rather than the per-image loop. That path built its own
    StatusDisplay and printed to stdout, so the bar rendered in whatever terminal
    the server happened to have and the web UI saw no progress at all."""

    def test_the_components_update_pmr_flashes_are_the_dual_cpu_pair(self):
        from flash_scripts._dual_cpu import find_dual_cpu_pair
        pair = find_dual_cpu_pair([_entry("pmr"), _entry("dir")])
        assert pair is not None
        assert (pair[0].component, pair[1].component) == ("pmr", "dir")

    def test_the_callers_display_is_used_not_a_fresh_one(self, monkeypatch):
        # Every step is stubbed: this pins the narration wiring, not the flash.
        import flash_scripts._dual_cpu as dual

        for name in [n for n in dir(dual) if n.startswith("step_")]:
            monkeypatch.setattr(dual, name, lambda sess, ctx: None)
        monkeypatch.setattr(dual, "get_script", lambda t: (object(), 0x00))

        seen: list = []
        d = QuietDisplay(lambda kind, payload: seen.append((kind, payload)))
        dual.run_pcs_dual_cpu(None, object(), _entry("pmr"), object(), _entry("dir"),
                              display=d)

        # Both images are announced on the caller's stream...
        statuses = [p["status"] for k, p in seen if k == "status"]
        assert any("pmr" in s for s in statuses)
        assert any("dir" in s for s in statuses)
        # ...and nothing was written straight to stdout.
        assert d.lines

    def test_each_image_starts_its_own_progress_span(self, monkeypatch):
        # The second image opens at 0% right after the first closed at 100%; a
        # -100 delta is under the throttle's threshold, so without a header
        # between them that first report would be dropped and the bar would sit
        # at 100% for the whole of CPU1.
        import flash_scripts._dual_cpu as dual

        transfers: list = []

        def _transfer(sess, ctx):
            ctx.display.set_progress(0, 8192, ctx.entry.component)
            ctx.display.set_progress(8192, 8192, ctx.entry.component)
            transfers.append(ctx.entry.component)

        for name in [n for n in dir(dual) if n.startswith("step_")]:
            monkeypatch.setattr(dual, name, lambda sess, ctx: None)
        monkeypatch.setattr(dual, "step_transfer_loop", _transfer)
        monkeypatch.setattr(dual, "get_script", lambda t: (object(), 0x00))

        seen: list = []
        dual.run_pcs_dual_cpu(None, object(), _entry("pmr"), object(), _entry("dir"),
                              display=QuietDisplay(lambda k, p: seen.append((k, p))))

        assert transfers == ["dir", "pmr"]          # secondary CPU first
        values = [p["value"] for k, p in seen if k == "progress"]
        assert values == [0.0, 100.0, 0.0, 100.0]   # neither 0% is throttled away


class TestUdsNodeFor:
    def test_a_bootloader_image_goes_through_its_parent_app_node(self):
        assert uds_node_for("vcsecbl") == "VCSEC"
        assert uds_node_for("pmrbu") == "PMR"

    def test_an_ordinary_component_is_its_own_node(self):
        assert uds_node_for("pmr") == "PMR"

    def test_an_explicit_default_is_used_when_nothing_else_maps(self):
        # UPDATE_HCML flashes 'hcml' but locks VCFRONT, because the headlamp is
        # reached through it.
        assert uds_node_for("nosuchthing", default="VCFRONT") == "VCFRONT"


class TestQuietDisplay:
    def test_lines_are_collected_when_there_is_no_terminal(self):
        d = QuietDisplay()
        d.set_header("flash pmr.bhx")
        d.set_detail("Erasing")
        d.finalize()
        assert d.lines == ["flash pmr.bhx", "  Erasing"]

    def test_steps_and_byte_progress_go_out_as_events(self):
        seen = []
        d = QuietDisplay(lambda kind, payload: seen.append((kind, payload)), label="PMR")
        d.set_detail("Transfer …")
        d.set_progress(2048, 8192, "SHDR 1/2")

        kinds = [k for k, _ in seen]
        assert kinds == ["status", "progress"]
        prog = seen[1][1]
        assert prog["value"] == 25.0        # a real percentage, not a rendered bar
        assert (prog["current"], prog["total"], prog["units"]) == (2048, 8192, "bytes")
        assert prog["source"] == "PMR"

    def test_progress_is_not_recorded_as_a_step(self):
        # A 216 KB image is ~850 transfer blocks. Logging each one buried the
        # actual steps under hundreds of redraws of the same bar.
        d = QuietDisplay()
        d.set_header("flash pmr.bhx")
        for sent in range(0, 8193, 256):
            d.set_progress(sent, 8192, "SHDR 1/1")
        assert d.lines == ["flash pmr.bhx"]

    def test_progress_events_are_throttled_not_one_per_block(self):
        seen = []
        d = QuietDisplay(lambda k, p: seen.append(p))
        for sent in range(0, 8193, 8):          # 1025 blocks, 0.1% apart
            d.set_progress(sent, 8192, "SHDR 1/1")
        # Emitting all of them stalled the run: the websocket pump has to drain
        # before the HTTP response goes out, so a finished flash sat there.
        assert len(seen) < 150
        assert seen[0]["value"] == 0.0
        assert seen[-1]["value"] == 100.0       # the last one is never dropped

    def test_a_new_image_restarts_the_progress_span(self):
        seen = []
        d = QuietDisplay(lambda k, p: seen.append(p))
        d.set_progress(8192, 8192, "first")
        d.set_header("flash dir.bhx")
        d.set_progress(0, 8192, "second")       # 0% after 100% must not be dropped
        assert [p["value"] for p in seen if p.get("units") == "bytes"] == [100.0, 0.0]

    def test_a_broken_listener_never_fails_the_flash(self):
        def boom(kind, payload):
            raise RuntimeError("listener died")

        d = QuietDisplay(boom)
        d.set_detail("still fine")          # must not raise
        assert d.lines == ["  still fine"]

    def test_progress_with_no_total_reports_no_percentage(self):
        seen = []
        QuietDisplay(lambda k, p: seen.append(p)).set_progress(0, 0)
        assert seen[0]["value"] is None


class _Session:
    """Just the DID read flash_components needs to identify the ECU."""

    def __init__(self, f180: bytes):
        self._f180 = f180

    def read_did(self, did):
        assert did == 0xF180
        return self._f180


def _bundle_tsv(tmp_path: Path, rows) -> Path:
    lines = ["\t".join([lk, src, dest, comp, "0", "*", ""]) for lk, src, dest, comp in rows]
    (tmp_path / "signed_metadata_map.tsv").write_text("\n".join(lines) + "\n")
    return tmp_path


class TestFlashComponents:
    def _run(self, tmp_path, components, rows, monkeypatch, **kw):
        import uds_local.identity as identity

        class _Ident:
            packed_key = 123
            lookup_key = "pmr:123"

        monkeypatch.setattr(identity, "parse_f180", lambda *a, **k: _Ident())
        monkeypatch.setattr("uds_local.identity.parse_f180", lambda *a, **k: _Ident())
        return flash_scripts.flash_components(
            _Session(b"\x00"), _bundle_tsv(tmp_path, rows), "pmr", components,
            dry_run=True, **kw)

    ROWS = [("pmr:123", "pmr/pmr.bhx", "pmr.bhx", "pmr"),
            ("pmr:123", "dir/dir.bhx", "dir.bhx", "dir")]

    def test_the_requested_components_are_selected_by_name(self, tmp_path, monkeypatch):
        res = self._run(tmp_path, ["pmr", "dir"], self.ROWS, monkeypatch)
        assert res["flashed"] == ["pmr", "dir"]
        assert res["ok"] is True

    def test_a_component_this_ecu_has_no_firmware_for_fails_the_request(
            self, tmp_path, monkeypatch):
        res = self._run(tmp_path, ["pmr", "dif"], self.ROWS, monkeypatch)
        assert res["unplaced"] == ["dif"]
        assert res["ok"] is False           # a partial flash is not a success

    def test_firmware_with_no_validated_sequence_is_reported_not_attempted(
            self, tmp_path, monkeypatch):
        rows = [*self.ROWS, ("pmr:123", "x/x.bhx", "x.bhx", "nosuchecu")]
        res = self._run(tmp_path, ["pmr", "nosuchecu"], rows, monkeypatch)
        assert res["no_script"] == ["nosuchecu"]
        assert "nosuchecu" not in res["flashed"]
        assert res["ok"] is False

    def test_a_missing_metadata_file_is_an_error_not_an_empty_flash(
            self, tmp_path, monkeypatch):
        with pytest.raises(FileNotFoundError, match="signed_metadata_map"):
            self._run(tmp_path / "empty", ["pmr"], [], monkeypatch)
