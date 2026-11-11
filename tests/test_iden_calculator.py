"""
Unit tests for IDEN Bandplan and Frequency Calculator
"""

import pytest
from dvm_nexus.config_engine.iden_calculator import (
    IdenEntry,
    parse_iden_table,
    format_iden_table,
    calculate_channel_number,
    calculate_frequency_from_channel,
    find_or_create_iden_entry,
)

SAMPLE_IDEN_TEXT = """# Sample table
0,851006250,6.25,-45.000,12.5,
2,450000000,6.25,5.000,12.5,
3,146000000,6.25,1.000,12.5,
"""


def test_parse_and_format_iden_table():
    entries = parse_iden_table(SAMPLE_IDEN_TEXT)
    assert len(entries) == 3
    assert entries[0].channel_id == 0
    assert entries[0].base_freq_hz == 851006250
    assert entries[0].offset_mhz == -45.0
    assert entries[1].channel_id == 2
    assert entries[1].base_freq_hz == 450000000

    formatted = format_iden_table(entries)
    assert "2,450000000,6.25,5.000,12.5," in formatted


def test_channel_calculation():
    base_freq_hz = 440000000
    spacing_khz = 6.25
    target_freq_hz = 446000000

    # 446.000 MHz is 6 MHz above 440.000 MHz.
    # 6,000,000 / 6,250 = 960 channels
    channel_no, actual_freq = calculate_channel_number(target_freq_hz, base_freq_hz, spacing_khz)
    assert channel_no == 960
    assert actual_freq == 446000000

    # Test reverse
    tx_freq, rx_freq = calculate_frequency_from_channel(channel_no, base_freq_hz, spacing_khz, offset_mhz=5.0)
    assert tx_freq == 446000000
    assert rx_freq == 451000000


def test_find_or_create_iden_entry():
    entries = parse_iden_table(SAMPLE_IDEN_TEXT)
    # Target UHF 446 MHz TX, 451 MHz RX (+5.0 MHz split)
    tx_hz = 446000000
    rx_hz = 451000000
    entry, ch_no = find_or_create_iden_entry(entries, tx_hz, rx_hz, spacing_khz=6.25)
    assert entry.offset_mhz == 5.0
    assert ch_no >= 1
    # Verify calculated TX frequency matches target
    assert entry.base_freq_hz + (ch_no * 6250) == tx_hz


def test_find_or_create_iden_entry_never_zero_offset():
    """Verify simplex (TX == RX) never generates 0.0 MHz offset which crashes DVMHost."""
    entries = []
    # Simplex GMRS/UHF 462.5625 MHz
    freq_hz = 462562500
    entry, ch_no = find_or_create_iden_entry(entries, freq_hz, freq_hz, spacing_khz=6.25)
    assert entry.offset_mhz != 0.0
    assert entry.offset_mhz == 5.0

    calc_tx, calc_rx = calculate_frequency_from_channel(ch_no, entry.base_freq_hz, entry.spacing_khz, entry.offset_mhz)
    assert calc_tx == freq_hz
    assert calc_rx == freq_hz + 5000000
